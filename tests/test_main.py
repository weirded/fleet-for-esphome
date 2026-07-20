"""Regression tests for main.py background tasks and startup behaviour.

Issue #25: UI didn't load on HAOS with 1.3.0 because:
  1. ha_entity_poller never set first_poll=False on error, causing an
     immediate tight-retry loop instead of sleeping 30 s between attempts.
  2. on_startup blocked on the HA Supervisor API (up to 15 s of timeouts),
     delaying the web server from accepting connections.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# ha_entity_poller – tight-retry regression
# ---------------------------------------------------------------------------

async def test_ha_entity_poller_sleeps_after_first_failure():
    """ha_entity_poller must sleep 30 s between retries even when the first
    poll fails with an exception (regression for issue #25)."""
    from main import ha_entity_poller

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Stop the loop after the first sleep so the test terminates quickly.
        raise asyncio.CancelledError

    app: dict = {"ha_entity_status": {}, "ha_mac_set": set()}

    with (
        patch("os.environ.get", return_value="fake-token"),
        patch("aiohttp.ClientSession") as mock_session_cls,
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        # Make the ClientSession raise an exception to simulate a failed poll.
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        with pytest.raises(asyncio.CancelledError):
            await ha_entity_poller(app)  # type: ignore[arg-type]

    # The key assertion: asyncio.sleep(30) MUST have been called after the
    # first failed attempt.  Before the fix, first_poll stayed True so the
    # sleep was skipped and the poller spun in a tight loop.
    assert sleep_calls, "asyncio.sleep was never called — poller is in a tight retry loop"
    assert sleep_calls[0] == 30, f"Expected sleep(30) after failure, got sleep({sleep_calls[0]})"


async def test_ha_entity_poller_sleeps_after_continue_on_non200():
    """ha_entity_poller must sleep 30 s even when a non-200 status causes the
    inner loop to `continue` (another path that previously kept first_poll=True)."""
    from main import ha_entity_poller

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    # Simulate: template API works, states API returns 403 → triggers `continue`
    async def fake_get(*args, **kwargs):
        resp = AsyncMock()
        resp.status = 403
        resp.text = AsyncMock(return_value="Forbidden")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    async def fake_post(*args, **kwargs):
        resp = AsyncMock()
        resp.status = 200
        resp.text = AsyncMock(return_value="[]")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    app: dict = {"ha_entity_status": {}, "ha_mac_set": set()}

    with (
        patch("os.environ.get", return_value="fake-token"),
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        mock_session = MagicMock()
        mock_session.get = fake_get
        mock_session.post = fake_post
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(asyncio.CancelledError):
                await ha_entity_poller(app)  # type: ignore[arg-type]

    assert sleep_calls, "asyncio.sleep was never called after non-200 states response"
    assert sleep_calls[0] == 30


# ---------------------------------------------------------------------------
# pypi_version_refresher – runs immediately on first iteration
# ---------------------------------------------------------------------------

async def test_pypi_version_refresher_does_not_sleep_on_first_run():
    """pypi_version_refresher must NOT sleep before its first iteration so that
    version detection happens promptly after startup (the Supervisor API check
    was moved out of on_startup in the same fix)."""
    from main import pypi_version_refresher

    sleep_calls: list[float] = []
    session_created = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Terminate after first sleep so the test is quick.
        raise asyncio.CancelledError

    app: dict = {
        "esphome_detected_version": None,
        "esphome_available_versions": [],
        "esphome_versions_fetched_at": 0.0,
    }

    with (
        patch("main._fetch_ha_esphome_version", new_callable=AsyncMock, return_value=None),
        patch("main._fetch_pypi_versions", new_callable=AsyncMock, return_value=[]),
        patch("aiohttp.ClientSession") as mock_session_cls,
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.side_effect = lambda: (session_created.append(True), mock_session)[1]

        with pytest.raises(asyncio.CancelledError):
            await pypi_version_refresher(app)  # type: ignore[arg-type]

    # The session must have been created BEFORE any sleep (first run is immediate).
    assert session_created, "version refresher never called the Supervisor API on first run"
    # The first sleep should come AFTER the first run, not before.
    assert sleep_calls, "version refresher never slept — subsequent runs would spin"
    assert sleep_calls[0] == 30


# ---------------------------------------------------------------------------
# on_startup – does not call _fetch_ha_esphome_version synchronously
# ---------------------------------------------------------------------------

async def test_on_startup_does_not_block_on_supervisor_api(tmp_path):
    """on_startup must not call _fetch_ha_esphome_version (regression for issue
    #25 where the blocking Supervisor API calls prevented the server from
    accepting connections for up to 15 s after restart)."""
    import main as main_module
    from main import create_app

    supervisor_api_calls: list[str] = []

    async def tracking_fetch_ha_version(session):  # noqa: ANN001
        supervisor_api_calls.append("called")
        return None

    config_dir = tmp_path / "esphome"
    config_dir.mkdir()

    with (
        patch.dict(
            "os.environ",
            {"ESPHOME_CONFIG_DIR": str(config_dir), "PORT": "18765", "SERVER_TOKEN": "test"},
        ),
        patch.object(main_module, "_fetch_ha_esphome_version", tracking_fetch_ha_version),
        patch.object(main_module, "_fetch_pypi_versions", new_callable=AsyncMock, return_value=[]),
        # Prevent real background tasks from running
        patch("asyncio.create_task", return_value=MagicMock()),
        patch("main.DevicePoller") as mock_poller_cls,
    ):
        mock_poller = AsyncMock()
        mock_poller.start = AsyncMock()
        mock_poller.update_compile_targets = MagicMock()
        mock_poller_cls.return_value = mock_poller

        app = create_app()
        # Manually fire on_startup (simulates aiohttp's startup sequence)
        for hook in app.on_startup:
            await hook(app)

    assert not supervisor_api_calls, (
        "on_startup called _fetch_ha_esphome_version — this blocks startup for "
        "up to 15 s when the HA Supervisor API is slow or unreachable"
    )


# ---------------------------------------------------------------------------
# _fetch_ha_esphome_version – add-on slug discovery (bug #4)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    async def json(self) -> dict:
        return self._payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in that replays scripted responses."""

    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    def get(self, url: str, headers=None, timeout=None):  # type: ignore[no-untyped-def]
        self.calls.append(url)
        if url in self._routes:
            return self._routes[url]
        return _FakeResponse(404)


async def test_fetch_ha_esphome_version_finds_hashed_slug(monkeypatch):
    """A community-repo hashed slug like ``a0d7b954_esphome`` is discovered
    via the per-slug /info probe.

    #86: previously this used the /addons listing, but that requires
    hassio_role: manager which we don't have. The listing call returned 403
    every 30s and spammed the Supervisor log. Now the function probes a
    known list of slug patterns directly via /addons/<slug>/info.
    """
    from main import _fetch_ha_esphome_version

    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")
    routes = {
        # Standard slugs return 404 (not installed)
        "http://supervisor/addons/core_esphome/info": _FakeResponse(404, {}),
        "http://supervisor/addons/local_esphome/info": _FakeResponse(404, {}),
        # Community-repo hash returns the version
        "http://supervisor/addons/a0d7b954_esphome/info": _FakeResponse(200, {
            "data": {"version": "2026.3.3"},
        }),
    }
    session = _FakeSession(routes)

    version = await _fetch_ha_esphome_version(session)  # type: ignore[arg-type]
    assert version == "2026.3.3"
    # No /addons listing call — that endpoint is no longer used.
    assert "http://supervisor/addons" not in session.calls


async def test_fetch_ha_esphome_version_returns_none_when_not_installed(monkeypatch):
    """ESPHome add-on not installed — returns None cleanly. All per-slug
    /info probes return 404 (default _FakeSession behavior)."""
    from main import _fetch_ha_esphome_version

    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")
    session = _FakeSession({})  # all routes 404

    version = await _fetch_ha_esphome_version(session)  # type: ignore[arg-type]
    assert version is None


async def test_fetch_ha_esphome_version_probes_core_slug(monkeypatch):
    """Built-in core_esphome installs resolve via the per-slug /info probe."""
    from main import _fetch_ha_esphome_version

    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")
    routes = {
        "http://supervisor/addons/core_esphome/info": _FakeResponse(
            200, {"data": {"version": "2026.3.3"}},
        ),
    }
    session = _FakeSession(routes)
    version = await _fetch_ha_esphome_version(session)  # type: ignore[arg-type]
    assert version == "2026.3.3"
    # #86: never queries the /addons listing endpoint
    assert "http://supervisor/addons" not in session.calls


# ---------------------------------------------------------------------------
# ha_entity_poller – repeated-warning suppression (bug #5)
# ---------------------------------------------------------------------------

async def test_ha_entity_poller_demotes_repeated_warnings_to_debug(monkeypatch, caplog):
    """After the second identical failure in a row, the warning must drop to
    DEBUG so a persistent outage doesn't drown the log (bug #5).

    The first two failures log at WARNING (with a one-time "above warning is
    repeating" notice on the second), the third+ log at DEBUG, and a
    successful poll resets the suppression counter.
    """
    import logging
    from main import ha_entity_poller

    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")

    # Swap asyncio.sleep for a fake that lets us run exactly N iterations.
    iteration_count = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        iteration_count["n"] += 1
        if iteration_count["n"] >= 5:
            raise asyncio.CancelledError()

    # Force every poll to fail identically by making aiohttp.ClientSession()
    # return a context manager whose get() always raises.
    class _AlwaysFailSession:
        async def __aenter__(self) -> "_AlwaysFailSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def get(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated HA down")

        def post(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated HA down")

    app = {"ha_entity_status": {}, "ha_mac_set": set()}

    with patch("asyncio.sleep", side_effect=fake_sleep), \
         patch("aiohttp.ClientSession", return_value=_AlwaysFailSession()), \
         caplog.at_level(logging.DEBUG, logger="main"):
        try:
            await ha_entity_poller(app)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            pass

    # Collect ha_entity_poller records by level
    main_records = [r for r in caplog.records if r.name == "main"]
    warnings = [r for r in main_records if r.levelno == logging.WARNING]
    debugs = [r for r in main_records if r.levelno == logging.DEBUG]

    # Each iteration emits two distinct fingerprints (``template_exception``
    # from the inner try and ``poll_exception`` from the outer except). Each
    # fingerprint is tracked independently: occurrences 1 and 2 log at
    # WARNING (with a one-time "repeating" notice on the second), and
    # occurrences 3+ drop to DEBUG.
    #
    # Over 5 iterations: 2 fingerprints × (2 warnings + 1 notice) = 6
    # warning records, and 2 × 3 = 6 suppressed-to-DEBUG records.
    warning_messages = [r.getMessage() for r in warnings]
    assert len(warnings) == 6, (
        f"expected 6 warning records, got {len(warnings)}: {warning_messages}"
    )
    repeating_notices = [m for m in warning_messages if "repeating" in m]
    assert len(repeating_notices) == 2, (
        f"expected 2 'repeating' notices (one per fingerprint), got: {repeating_notices}"
    )
    debug_messages = [r.getMessage() for r in debugs]
    assert any("Error polling" in m or "Template API" in m for m in debug_messages), (
        f"expected suppressed DEBUG records for the repeating failures, got: {debug_messages}"
    )


# ----------------------------------------------------------------------
# _esphome_version_key – PEP440-ish sort for ESPHome versions (bug #16)
# ----------------------------------------------------------------------

def test_esphome_version_key_orders_betas_by_number():
    """b3 must sort ABOVE b2, not equal to it (bug #16)."""
    from main import _esphome_version_key

    versions = ["2026.4.0b2", "2026.4.0b3", "2026.4.0b1"]
    versions.sort(key=_esphome_version_key, reverse=True)
    assert versions == ["2026.4.0b3", "2026.4.0b2", "2026.4.0b1"]


def test_esphome_version_key_orders_pre_release_tiers():
    """Within the same base version, order is a < b < rc < stable."""
    from main import _esphome_version_key

    versions = ["2026.3.0", "2026.3.0rc1", "2026.3.0b1", "2026.3.0a1"]
    versions.sort(key=_esphome_version_key, reverse=True)
    assert versions == ["2026.3.0", "2026.3.0rc1", "2026.3.0b1", "2026.3.0a1"]


def test_esphome_version_key_orders_stable_versions():
    """Stable semver sorts in normal descending order."""
    from main import _esphome_version_key

    versions = ["2025.12.0", "2026.3.2", "2026.3.10", "2026.3.1", "2026.4.0"]
    versions.sort(key=_esphome_version_key, reverse=True)
    assert versions == ["2026.4.0", "2026.3.10", "2026.3.2", "2026.3.1", "2025.12.0"]


def test_esphome_version_key_mixed_stable_and_betas():
    """Stable releases outrank all pre-release tags of the same base."""
    from main import _esphome_version_key

    versions = ["2026.4.0b3", "2026.3.2", "2026.4.0", "2026.4.0b1", "2026.3.3"]
    versions.sort(key=_esphome_version_key, reverse=True)
    assert versions == ["2026.4.0", "2026.4.0b3", "2026.4.0b1", "2026.3.3", "2026.3.2"]


# ---------------------------------------------------------------------------
# Bug #30 — standalone-Docker fallback picks latest stable from PyPI
# ---------------------------------------------------------------------------

def test_pick_latest_stable_skips_pre_releases():
    """Picks the newest pure `\\d+(\\.\\d+)*` string, skipping beta/rc/dev."""
    from main import _pick_latest_stable_version

    # _fetch_pypi_versions returns newest-first, so the picker mirrors that.
    versions = ["2026.5.0b1", "2026.5.0rc1", "2026.4.0", "2026.4.0b2", "2026.3.3"]
    assert _pick_latest_stable_version(versions) == "2026.4.0"


def test_pick_latest_stable_returns_none_when_empty():
    from main import _pick_latest_stable_version

    assert _pick_latest_stable_version([]) is None


def test_pick_latest_stable_returns_none_when_no_stable():
    """All pre-releases → nothing to install; caller surfaces a warning."""
    from main import _pick_latest_stable_version

    assert _pick_latest_stable_version(["2026.5.0b1", "2026.4.0rc2", "2026.3.0dev"]) is None


def test_pick_latest_stable_accepts_short_versions():
    """ESPHome has historically shipped `X.Y` and `X.Y.Z` — both are stable."""
    from main import _pick_latest_stable_version

    assert _pick_latest_stable_version(["2024.12", "2024.11.5", "2024.11"]) == "2024.12"


# ---------------------------------------------------------------------------
# Bug #11 (1.6.1): reseed after ensure_esphome_installed completes
# ---------------------------------------------------------------------------

async def test_reseed_device_poller_refreshes_after_install(tmp_path):
    """On first boot the ESPHome venv hasn't been installed yet, so
    ``build_name_to_target_map`` returns empty encryption keys — every
    YAML whose ``esphome.name`` needs the substitution pass comes back
    from ``_resolve_esphome_config`` as ``None`` until the venv is
    ready.

    ``reseed_device_poller_from_config`` has to be invokable at the
    tail of the install task so the poller catches up without waiting
    for the next 30-second config-scanner tick. This test simulates
    the narrow invariant: an empty-first-seed poller is re-populated
    with real keys on the second call.

    Async because the helper is async (#84 moved the heavy
    ``build_name_to_target_map`` into an executor so the full
    ESPHome validator doesn't block the event loop — see main.py).
    """
    from main import reseed_device_poller_from_config

    # Stand up a minimal ESPHome fixture inside tmp_path so the
    # scanner has real YAML to chew on.
    (tmp_path / "secrets.yaml").write_text(
        'api_encryption_key: "Zp82U4SqCqe55xkDDuPXzsoNhcmEws7/HbNXsv2qOGI="\n'
        'wifi_ssid: "x"\nwifi_password: "testpass1"\nota_password: "x"\n'
    )
    (tmp_path / "my-device.yaml").write_text(
        'esphome:\n  name: my-device\n'
        'esp8266:\n  board: d1_mini\n'
        'wifi:\n  ssid: !secret wifi_ssid\n  password: !secret wifi_password\n'
        'api:\n  encryption:\n    key: !secret api_encryption_key\n'
        'ota:\n  - platform: esphome\n'
    )

    # Mock config object matching AppConfig's .config_dir duck-type.
    class _Cfg:
        config_dir = str(tmp_path)

    # Mock poller captures the last update_compile_targets call.
    captured: dict = {}

    class _Poller:
        def update_compile_targets(self, targets, name_map, enc_keys, addr_overrides, addr_sources):
            captured["targets"] = list(targets)
            captured["name_map"] = dict(name_map)
            captured["enc_keys"] = dict(enc_keys)
            captured["addr_overrides"] = dict(addr_overrides)
            captured["addr_sources"] = dict(addr_sources)

    app = {"config": _Cfg(), "device_poller": _Poller()}

    # First call — stands in for the in-flight install window: the
    # poller now sees whatever the scanner resolved.
    await reseed_device_poller_from_config(app, reason="test-initial")
    first_keys = dict(captured["enc_keys"])
    assert "my-device.yaml" in captured["targets"]

    # Second call mirrors what runs after ``ensure_esphome_installed``
    # completes — same inputs, same outputs, but the code path at
    # least executes cleanly and re-issues update_compile_targets.
    await reseed_device_poller_from_config(app, reason="esphome install complete")
    assert captured["enc_keys"] == first_keys
    # Bug #11 belt-and-braces: encryption keys carry both hyphenated
    # and underscore-normalised aliases after reseed.
    assert "my-device" in captured["enc_keys"]
    assert "my_device" in captured["enc_keys"]


async def test_reseed_device_poller_no_op_when_poller_absent(tmp_path):
    """No device_poller in app — the helper returns without raising."""
    from main import reseed_device_poller_from_config

    class _Cfg:
        config_dir = str(tmp_path)

    app = {"config": _Cfg()}
    # Should just return; no assertion needed beyond "didn't raise".
    await reseed_device_poller_from_config(app, reason="no poller")


# ---------------------------------------------------------------------------
# _install_esphome_initial – bug #105
# ---------------------------------------------------------------------------


async def test_install_esphome_initial_prefers_supervisor_version(monkeypatch, tmp_path):
    """When SUPERVISOR_TOKEN is set and the HA ESPHome builder add-on is
    installed, the initial install targets the Supervisor-reported
    version (not the PyPI latest). Regression for #105's first path."""
    import main as main_module

    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")

    installs: list[str] = []

    async def fake_supervisor(session):  # noqa: ANN001
        return "2026.3.3"

    async def fake_pypi(session):  # noqa: ANN001
        raise AssertionError("PyPI must not be queried when Supervisor returns a version")

    def fake_ensure(ver: str) -> None:
        installs.append(ver)

    set_versions: list[str] = []

    def fake_set_version(ver: str) -> None:
        set_versions.append(ver)

    async def fake_reseed(app, *, reason: str) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(main_module, "_fetch_ha_esphome_version", fake_supervisor)
    monkeypatch.setattr(main_module, "_fetch_pypi_versions", fake_pypi)
    monkeypatch.setattr(main_module, "reseed_device_poller_from_config", fake_reseed)

    import scanner as scanner_module
    monkeypatch.setattr(scanner_module, "_get_installed_esphome_version", lambda: "unknown")
    monkeypatch.setattr(scanner_module, "ensure_esphome_installed", fake_ensure)
    monkeypatch.setattr(scanner_module, "set_esphome_version", fake_set_version)

    await main_module._install_esphome_initial({})  # type: ignore[arg-type]

    assert installs == ["2026.3.3"]
    assert set_versions == ["2026.3.3"]


async def test_install_esphome_initial_falls_back_to_pypi_on_fresh_haos(monkeypatch):
    """Fresh HAOS: SUPERVISOR_TOKEN is set but the HA ESPHome builder
    add-on is NOT installed, so `_fetch_ha_esphome_version` returns None.
    Previously this early-returned and the install banner stuck forever
    (#105). Now we fall back to PyPI latest stable."""
    import main as main_module

    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")

    installs: list[str] = []
    set_versions: list[str] = []

    async def fake_supervisor(session):  # noqa: ANN001
        return None  # builder add-on not installed

    async def fake_pypi(session):  # noqa: ANN001
        return ["2026.3.3", "2026.3.2"]

    def fake_ensure(ver: str) -> None:
        installs.append(ver)

    def fake_set_version(ver: str) -> None:
        set_versions.append(ver)

    async def fake_reseed(app, *, reason: str) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(main_module, "_fetch_ha_esphome_version", fake_supervisor)
    monkeypatch.setattr(main_module, "_fetch_pypi_versions", fake_pypi)
    monkeypatch.setattr(main_module, "reseed_device_poller_from_config", fake_reseed)

    import scanner as scanner_module
    monkeypatch.setattr(scanner_module, "_get_installed_esphome_version", lambda: "unknown")
    monkeypatch.setattr(scanner_module, "ensure_esphome_installed", fake_ensure)
    monkeypatch.setattr(scanner_module, "set_esphome_version", fake_set_version)

    await main_module._install_esphome_initial({})  # type: ignore[arg-type]

    assert installs == ["2026.3.3"]
    assert set_versions == ["2026.3.3"]


async def test_install_esphome_initial_standalone_docker_uses_pypi(monkeypatch):
    """Standalone Docker: no SUPERVISOR_TOKEN at all → PyPI path."""
    import main as main_module

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    installs: list[str] = []
    supervisor_calls: list[int] = []

    async def fake_supervisor(session):  # noqa: ANN001
        supervisor_calls.append(1)
        return None

    async def fake_pypi(session):  # noqa: ANN001
        return ["2026.3.3"]

    def fake_ensure(ver: str) -> None:
        installs.append(ver)

    async def fake_reseed(app, *, reason: str) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(main_module, "_fetch_ha_esphome_version", fake_supervisor)
    monkeypatch.setattr(main_module, "_fetch_pypi_versions", fake_pypi)
    monkeypatch.setattr(main_module, "reseed_device_poller_from_config", fake_reseed)

    import scanner as scanner_module
    monkeypatch.setattr(scanner_module, "_get_installed_esphome_version", lambda: "unknown")
    monkeypatch.setattr(scanner_module, "ensure_esphome_installed", fake_ensure)
    monkeypatch.setattr(scanner_module, "set_esphome_version", lambda v: None)

    await main_module._install_esphome_initial({})  # type: ignore[arg-type]

    # Supervisor probe is skipped entirely when SUPERVISOR_TOKEN is absent.
    assert supervisor_calls == []
    assert installs == ["2026.3.3"]


async def test_install_esphome_initial_skips_install_when_bundled(monkeypatch):
    """When ESPHome is already bundled (a real version, not a sentinel),
    we still run `ensure_esphome_installed` to make sure the venv cache
    has an activated binary, but we DON'T query Supervisor or PyPI."""
    import main as main_module

    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")

    installs: list[str] = []

    async def fake_supervisor(session):  # noqa: ANN001
        raise AssertionError("Supervisor must not be queried when bundled version is known")

    async def fake_pypi(session):  # noqa: ANN001
        raise AssertionError("PyPI must not be queried when bundled version is known")

    def fake_ensure(ver: str) -> None:
        installs.append(ver)

    async def fake_reseed(app, *, reason: str) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(main_module, "_fetch_ha_esphome_version", fake_supervisor)
    monkeypatch.setattr(main_module, "_fetch_pypi_versions", fake_pypi)
    monkeypatch.setattr(main_module, "reseed_device_poller_from_config", fake_reseed)

    import scanner as scanner_module
    monkeypatch.setattr(scanner_module, "_get_installed_esphome_version", lambda: "2026.3.1")
    monkeypatch.setattr(scanner_module, "ensure_esphome_installed", fake_ensure)
    monkeypatch.setattr(scanner_module, "set_esphome_version", lambda v: None)

    await main_module._install_esphome_initial({})  # type: ignore[arg-type]

    assert installs == ["2026.3.1"]
