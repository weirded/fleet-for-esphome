"""Tests for the browser-facing UI API (/ui/api/*) in ui_api.py.

UI endpoints are unauthenticated (they rely on HA Ingress trust) so the
test client doesn't need auth headers.  Uses a test-local aiohttp app
with in-memory Queue/Registry and a tmp_path config dir.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import ui_api as ui_api_module
from app_config import AppConfig
from job_queue import JobQueue, JobState
from main import auth_middleware
from registry import WorkerRegistry

# pytest-homeassistant-custom-component (installed on some dev boxes but
# not in the plain CI test env) pulls in pytest-socket, which globally
# blocks socket() so aiohttp's TestServer can't bind a loopback listener.
# Per-test opt-in — only the tests that explicitly pull ``_enable_socket``
# as a fixture get loopback-socket access. Keeping it opt-in (rather than
# autouse for this whole module) avoids exposing unrelated cleanup races
# in older save/rename/delete tests that pre-date pytest-homeassistant
# landing on the dev machine.
@pytest.fixture
def _enable_socket():
    try:
        import pytest_socket as _pytest_socket  # type: ignore[import-not-found]
    except ImportError:
        yield
        return
    _pytest_socket.enable_socket()
    yield
    # Don't re-disable on teardown — asyncio cleanup during test exit
    # still needs the event loop's self-pipe socket (same reason as
    # test_worker_log_endpoints.py).


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _UiApp:
    """Container for a running TestClient plus direct access to the app state."""

    def __init__(
        self,
        client: TestClient,
        cfg: AppConfig,
        queue: JobQueue,
        registry: WorkerRegistry,
        config_dir: Path,
        app: web.Application | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.queue = queue
        self.registry = registry
        self.config_dir = config_dir
        self.app = app

    async def close(self) -> None:
        await self.client.close()

    async def get(self, *args, **kwargs):
        return await self.client.get(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self.client.post(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self.client.delete(*args, **kwargs)


async def _make_ui_app(tmp_path: Path) -> _UiApp:
    """Spin up a fresh isolated UI test app."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    cfg = AppConfig(config_dir=str(config_dir))
    # SP.8: server token now lives in Settings, not AppConfig. Ensure
    # the scratch settings module has it set for auth-requiring tests.
    import settings as _settings_mod
    _settings_mod._reset_for_tests()
    _settings_mod.init_settings(
        settings_path=tmp_path / "settings.json",
        options_path=tmp_path / "options.json",
    )
    # #98: tests in this module exercise the active versioning path
    # (file history, rollback, commit endpoints). The dataclass
    # default is now ``'unset'``, which makes ``_versioning_active``
    # return False and turns every git op into a no-op. Flip to
    # ``'on'`` so the existing tests keep their behaviour.
    await _settings_mod.update_settings({"versioning_enabled": "on"})
    await _settings_mod.update_settings({"server_token": "ui-test-token"})
    queue = JobQueue(queue_file=tmp_path / "queue.json")
    registry = WorkerRegistry()

    app = web.Application(middlewares=[auth_middleware])
    app["config"] = cfg
    app["queue"] = queue
    app["registry"] = registry
    app["log_subscribers"] = {}
    # DQ.5: every UI test rig gets a real WorkerDiskQuotaStore so the
    # disk-quota endpoint exercises the full persistence path rather than
    # the graceful-degrade fallback.
    from worker_disk_quotas import WorkerDiskQuotaStore  # noqa: PLC0415
    app["worker_disk_quota_store"] = WorkerDiskQuotaStore(
        path=tmp_path / "worker-disk-quotas.json",
    )
    app["_rt"] = {
        "ha_entity_status": {},
        "ha_mac_set": set(),
        "ha_mac_to_device_id": {},
        "ha_name_to_device_id": {},
        "esphome_detected_version": None,
        "esphome_available_versions": [],
        "esphome_versions_fetched_at": 0.0,
        "schedule_checker_started_at": None,
        "schedule_checker_tick_count": 0,
        "schedule_checker_last_tick": None,
        "schedule_checker_last_error": None,
    }
    app.router.add_routes(ui_api_module.routes)

    client = TestClient(TestServer(app))
    await client.start_server()
    return _UiApp(client, cfg, queue, registry, config_dir, app=app)


def _write_config(config_dir: Path, filename: str, name: str) -> Path:
    """Write a minimal compilable ESPHome YAML config into the test config dir."""
    path = config_dir / filename
    path.write_text(f"esphome:\n  name: {name}\n\nesp8266:\n  board: d1_mini\n")
    return path


# ---------------------------------------------------------------------------
# server-info
# ---------------------------------------------------------------------------

async def test_server_info_returns_version_and_token(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/server-info")
        assert resp.status == 200
        data = await resp.json()
        assert data["token"] == "ui-test-token"
        assert "addon_version" in data
        # Bug #21 (1.6.1): read from the constant instead of hardcoding a
        # version so a future ``MIN_IMAGE_VERSION`` bump (like #6's 5→7)
        # doesn't regress this test. The /server-info endpoint must echo
        # whatever the live constant says, period.
        from constants import MIN_IMAGE_VERSION  # noqa: PLC0415
        assert "min_image_version" in data
        assert data["min_image_version"] == MIN_IMAGE_VERSION
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------

async def test_targets_lists_yaml_files(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "living_room.yaml", "living-room")
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")

        resp = await ta.get("/ui/api/targets")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        filenames = {t["target"] for t in data}
        assert "living_room.yaml" in filenames
        assert "bedroom.yaml" in filenames
    finally:
        await ta.close()


def test_parse_device_compile_epoch_handles_modern_aioesphomeapi_format():
    """Bug #102: the dev.18 fallback parser was hard-coded for the older
    ``"Mar 29 2026, 17:00:00"`` shape, but ``aioesphomeapi`` actually
    reports the build time as ``"2026-04-23 06:13:56 -0700"`` — so the
    parser silently returned None for every device and the Devices-tab
    "Last compiled" column never showed the device-firmware fallback.
    Round-trip through epoch + UTC ensures the offset is honoured."""
    from datetime import datetime, timezone, timedelta
    from ui_api import _parse_device_compile_epoch

    epoch = _parse_device_compile_epoch("2026-04-23 06:13:56 -0700")
    assert epoch is not None
    expected = datetime(2026, 4, 23, 6, 13, 56,
                        tzinfo=timezone(timedelta(hours=-7)))
    assert epoch == int(expected.timestamp())


def test_parse_device_compile_epoch_back_compat_old_format():
    """The older ``"%b %d %Y, %H:%M:%S"`` form must still parse so that
    devices running pre-aioesphomeapi-update firmware don't lose their
    fallback when the new format support lands."""
    from ui_api import _parse_device_compile_epoch
    assert _parse_device_compile_epoch("Mar 29 2026, 17:00:00") is not None


def test_parse_device_compile_epoch_returns_none_for_unknown_shapes():
    """Empty / None / unrecognised → None; never raises."""
    from ui_api import _parse_device_compile_epoch
    assert _parse_device_compile_epoch(None) is None
    assert _parse_device_compile_epoch("") is None
    assert _parse_device_compile_epoch("not a timestamp") is None
    assert _parse_device_compile_epoch("2026-04-23T06:13:56Z") is None


async def test_targets_excludes_secrets_yaml(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        (ta.config_dir / "secrets.yaml").write_text("wifi_password: secret")

        resp = await ta.get("/ui/api/targets")
        data = await resp.json()
        assert any(t["target"] == "device1.yaml" for t in data)
        assert not any(t["target"] == "secrets.yaml" for t in data)
    finally:
        await ta.close()


async def test_targets_config_modified_reflects_git_status_when_clean(tmp_path, _settings_init, _enable_socket):
    """In a git repo, `config_modified` reflects `git status` (uncommitted
    edits) — NOT file mtime. The user's mental model for "changed locally"
    is `git status`, and mtime false-positives on editor autosaves, external
    touches, and `git checkout`. A target that's been committed and has no
    past successful flash on record must not show as modified."""
    import git_versioning as gv
    gv._reset_for_tests()
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)
        # init_repo auto-commits an initial snapshot, so the tree is clean.

        resp = await ta.get("/ui/api/targets")
        assert resp.status == 200
        data = await resp.json()
        bedroom = next(t for t in data if t["target"] == "bedroom.yaml")
        assert bedroom["config_drifted_since_flash"] is None  # no past flash
        assert bedroom["has_uncommitted_changes"] is False
        assert bedroom["config_modified"] is False
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_targets_config_modified_reflects_git_status_when_dirty(tmp_path, _settings_init, _enable_socket):
    """Same setup as the clean variant above, but with an uncommitted edit:
    `config_modified` flips to True and matches `has_uncommitted_changes`."""
    import git_versioning as gv
    gv._reset_for_tests()
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        # External edit — bypass the committing save endpoint on purpose so
        # the tree ends up dirty, matching a user who edited via a shell.
        (ta.config_dir / "bedroom.yaml").write_text(
            "esphome:\n  name: bedroom\n# external edit\n"
        )

        resp = await ta.get("/ui/api/targets")
        data = await resp.json()
        bedroom = next(t for t in data if t["target"] == "bedroom.yaml")
        assert bedroom["has_uncommitted_changes"] is True
        assert bedroom["config_modified"] is True
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_targets_config_modified_uses_mtime_without_repo(tmp_path, _settings_init, _enable_socket):
    """When the config dir isn't a git repo, `config_modified` still falls
    back to the mtime-vs-compilation_time signal for non-versioning users.
    With no device attached we expect None (no compilation_time to compare
    against), which proves the mtime branch is taken and doesn't crash
    rather than short-circuiting to False from the git-status branch."""
    ta = await _make_ui_app(tmp_path)
    try:
        # Flip versioning off so `_versioning_active` returns False and
        # `head_hash` is None → mtime branch in ui_api._build_targets.
        import settings as settings_mod
        await settings_mod.update_settings({"versioning_enabled": "off"})

        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")

        resp = await ta.get("/ui/api/targets")
        data = await resp.json()
        bedroom = next(t for t in data if t["target"] == "bedroom.yaml")
        # No device_poller in the test app → no compilation_time → no mtime
        # comparison possible → None (not False).
        assert bedroom["config_modified"] is None
        assert bedroom["has_uncommitted_changes"] is False
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# config CRUD — get/save/delete content
# ---------------------------------------------------------------------------

async def test_get_target_content_returns_yaml(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        resp = await ta.get("/ui/api/targets/device1.yaml/content")
        assert resp.status == 200
        data = await resp.json()
        assert "esphome:" in data["content"]
        assert "device1" in data["content"]
    finally:
        await ta.close()


async def test_get_target_content_not_found(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/targets/missing.yaml/content")
        assert resp.status == 404
    finally:
        await ta.close()


async def test_get_target_content_rejects_path_traversal(tmp_path):
    """Attempting to read files outside the config dir must be refused."""
    ta = await _make_ui_app(tmp_path)
    try:
        # Encode the traversal so the URL parser doesn't strip it
        resp = await ta.get("/ui/api/targets/..%2Fsecret.txt/content")
        # Server should return 400 or 404, never 200 with the file contents
        assert resp.status in (400, 404)
    finally:
        await ta.close()


async def test_save_target_content_writes_file(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        new_content = "esphome:\n  name: renamed\n\nesp32:\n  board: esp32dev\n"
        resp = await ta.post(
            "/ui/api/targets/device1.yaml/content",
            json={"content": new_content},
        )
        assert resp.status == 200
        assert (ta.config_dir / "device1.yaml").read_text() == new_content
    finally:
        await ta.close()


async def test_save_target_content_rejects_path_traversal(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post(
            "/ui/api/targets/..%2Fevil.yaml/content",
            json={"content": "pwned"},
        )
        assert resp.status in (400, 404)
        # Sanity: the file didn't get created
        assert not (tmp_path / "evil.yaml").exists()
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# delete / archive
# ---------------------------------------------------------------------------

async def test_delete_target_archives_by_default(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        resp = await ta.delete("/ui/api/targets/device1.yaml")
        assert resp.status == 200

        # File moved to .archive/
        assert not (ta.config_dir / "device1.yaml").exists()
        assert (ta.config_dir / ".archive" / "device1.yaml").exists()
    finally:
        await ta.close()


async def test_delete_target_permanent(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        resp = await ta.delete("/ui/api/targets/device1.yaml?archive=false")
        assert resp.status == 200
        assert not (ta.config_dir / "device1.yaml").exists()
        assert not (ta.config_dir / ".archive" / "device1.yaml").exists()
    finally:
        await ta.close()


async def test_delete_target_cancels_pending_jobs(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        job = await ta.queue.enqueue("device1.yaml", "2024.3.1", "run1", 300)
        assert job.state == JobState.PENDING

        resp = await ta.delete("/ui/api/targets/device1.yaml")
        assert resp.status == 200

        stored = ta.queue.get(job.id)
        assert stored.state == JobState.CANCELLED  # #49: cancel marks as CANCELLED
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# archive list / restore / permanent delete
# ---------------------------------------------------------------------------

async def test_archive_list_empty(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/archive")
        assert resp.status == 200
        assert await resp.json() == []
    finally:
        await ta.close()


async def test_archive_list_after_delete(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        await ta.delete("/ui/api/targets/device1.yaml")

        resp = await ta.get("/ui/api/archive")
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["filename"] == "device1.yaml"
    finally:
        await ta.close()


async def test_archive_restore(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        await ta.delete("/ui/api/targets/device1.yaml")
        assert not (ta.config_dir / "device1.yaml").exists()

        resp = await ta.post("/ui/api/archive/device1.yaml/restore")
        assert resp.status == 200
        assert (ta.config_dir / "device1.yaml").exists()
        assert not (ta.config_dir / ".archive" / "device1.yaml").exists()
    finally:
        await ta.close()


async def test_archive_permanent_delete(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        await ta.delete("/ui/api/targets/device1.yaml")

        resp = await ta.delete("/ui/api/archive/device1.yaml")
        assert resp.status == 200
        assert not (ta.config_dir / ".archive" / "device1.yaml").exists()
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# DM.1: /ui/api/targets merges archived rows + archive flag on every row
# ---------------------------------------------------------------------------

async def test_targets_marks_active_rows_archived_false(tmp_path, _enable_socket):
    """DM.1: every active row carries archived=False so the UI can
    render rows uniformly (opacity-50 / reduced action menu drives off
    this single flag)."""
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "alpha.yaml", "alpha")
        resp = await ta.get("/ui/api/targets")
        data = await resp.json()
        active = [t for t in data if t["target"] == "alpha.yaml"]
        assert len(active) == 1
        assert active[0]["archived"] is False
    finally:
        await ta.close()


async def test_targets_includes_archived_rows(tmp_path, _enable_socket):
    """DM.1: archived YAMLs land in /ui/api/targets so the Devices tab
    can render them inline (toggleable via the column picker). Each
    archived row has archived=True plus archived_at/archived_size."""
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "alpha.yaml", "alpha")
        _write_config(ta.config_dir, "beta.yaml", "beta")
        # Archive beta
        resp = await ta.delete("/ui/api/targets/beta.yaml")
        assert resp.status == 200

        resp = await ta.get("/ui/api/targets")
        data = await resp.json()
        rows_by_name = {t["target"]: t for t in data}

        assert "alpha.yaml" in rows_by_name
        assert rows_by_name["alpha.yaml"]["archived"] is False

        assert "beta.yaml" in rows_by_name
        beta = rows_by_name["beta.yaml"]
        assert beta["archived"] is True
        assert isinstance(beta["archived_at"], (int, float))
        assert beta["archived_size"] > 0
    finally:
        await ta.close()


async def test_targets_archived_rows_minimal_fields(tmp_path, _enable_socket):
    """DM.1: archived rows must carry the structural fields the UI
    expects (online/running_version null, no schedule, etc.) — the
    poller / scheduler / queue do not see them, so live-state fields
    are explicitly null."""
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        await ta.delete("/ui/api/targets/device1.yaml")

        resp = await ta.get("/ui/api/targets")
        data = await resp.json()
        archived = [t for t in data if t["archived"]]
        assert len(archived) == 1
        row = archived[0]
        assert row["target"] == "device1.yaml"
        assert row["online"] is None
        assert row["running_version"] is None
        assert row["schedule"] is None
        assert row["schedule_enabled"] is False
        assert row["last_compile"] is None
    finally:
        await ta.close()


async def test_archive_evicts_device_from_poller(tmp_path, _enable_socket):
    """DM.1: archiving a target must evict the matching entry from the
    device poller so the freshly-archived row freezes at last_seen=now
    instead of staying ``online`` for the 4 h TTL window. Mirrors the
    eviction the rename path already does. Origin: spec note in
    WORKITEMS-1.7.0 DM.1."""
    from datetime import datetime
    from device_poller import Device, DevicePoller

    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")

        poller = DevicePoller()
        poller._devices["device1"] = Device(
            name="device1",
            ip_address="192.168.1.42",
            online=True,
            last_seen=datetime.now(),
            compile_target="device1.yaml",
        )
        # The TestServer wraps the app in a frozen state by start; mutate
        # via the underlying _state dict to avoid the deprecation noise
        # while the test still hangs the poller off the same key the
        # handler reads.
        ta.client.server.app._state["device_poller"] = poller
        assert "device1" in poller._devices

        resp = await ta.delete("/ui/api/targets/device1.yaml")
        assert resp.status == 200
        assert "device1" not in poller._devices, (
            "archive should evict the device from the poller"
        )
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------

async def test_compile_all_enqueues_all_targets(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        _write_config(ta.config_dir, "b.yaml", "b")

        resp = await ta.post("/ui/api/compile", json={"targets": "all"})
        assert resp.status == 200
        data = await resp.json()
        assert data["enqueued"] == 2
        assert "run_id" in data

        jobs = ta.queue.get_all()
        targets = {j.target for j in jobs}
        assert targets == {"a.yaml", "b.yaml"}
    finally:
        await ta.close()


async def test_compile_specific_targets(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        _write_config(ta.config_dir, "b.yaml", "b")
        _write_config(ta.config_dir, "c.yaml", "c")

        resp = await ta.post("/ui/api/compile", json={"targets": ["a.yaml", "c.yaml"]})
        data = await resp.json()
        assert data["enqueued"] == 2

        targets = {j.target for j in ta.queue.get_all()}
        assert targets == {"a.yaml", "c.yaml"}
    finally:
        await ta.close()


async def test_compile_filters_unknown_targets(tmp_path):
    """A target not in the config dir is silently dropped, not an error."""
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        resp = await ta.post("/ui/api/compile", json={"targets": ["a.yaml", "ghost.yaml"]})
        data = await resp.json()
        assert data["enqueued"] == 1
        assert {j.target for j in ta.queue.get_all()} == {"a.yaml"}
    finally:
        await ta.close()


async def test_compile_pinned_client_id(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        resp = await ta.post(
            "/ui/api/compile",
            json={"targets": ["a.yaml"], "pinned_client_id": "worker-42"},
        )
        assert resp.status == 200
        jobs = ta.queue.get_all()
        assert len(jobs) == 1
        assert jobs[0].pinned_client_id == "worker-42"
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# DM.3: per-job OTA address override
# ---------------------------------------------------------------------------


async def test_compile_address_override_stamps_job_ota_address(tmp_path, _enable_socket):
    """DM.3: ``address`` body field overrides ``Job.ota_address``."""
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        resp = await ta.post(
            "/ui/api/compile",
            json={"targets": ["a.yaml"], "address": "192.168.42.7"},
        )
        assert resp.status == 200
        jobs = ta.queue.get_all()
        assert len(jobs) == 1
        assert jobs[0].ota_address == "192.168.42.7"
    finally:
        await ta.close()


async def test_compile_address_override_rejects_multi_target(tmp_path, _enable_socket):
    """DM.3: address + multi-target is meaningless and 400's."""
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        _write_config(ta.config_dir, "b.yaml", "b")
        resp = await ta.post(
            "/ui/api/compile",
            json={"targets": ["a.yaml", "b.yaml"], "address": "192.168.42.7"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "address override requires exactly one target" in body["error"]
        assert ta.queue.get_all() == []
    finally:
        await ta.close()


async def test_compile_address_too_long_returns_400(tmp_path, _enable_socket):
    """DM.3: address bound at 253 chars (DNS hostname limit)."""
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        resp = await ta.post(
            "/ui/api/compile",
            json={"targets": ["a.yaml"], "address": "x" * 254},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "address too long" in body["error"]
    finally:
        await ta.close()


async def test_compile_address_empty_falls_through_to_auto_resolve(tmp_path, _enable_socket):
    """DM.3: an empty/whitespace address is treated as "no override" — the
    auto-resolved value (or none) is used as if the field was absent."""
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        resp = await ta.post(
            "/ui/api/compile",
            json={"targets": ["a.yaml"], "address": "   "},
        )
        assert resp.status == 200
        jobs = ta.queue.get_all()
        assert len(jobs) == 1
        # No poller in test app + no override → ota_address is None.
        assert jobs[0].ota_address is None
    finally:
        await ta.close()


async def test_compile_invalid_json(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post(
            "/ui/api/compile",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

async def test_validate_runs_esphome_config_directly(tmp_path):
    """Bug #25: /ui/api/validate runs ``esphome config`` as a direct subprocess
    on the server. No queue, no worker, immediate response.

    We mock ``asyncio.create_subprocess_exec`` since ``esphome`` isn't
    installed in the test environment.
    """
    from unittest.mock import AsyncMock, patch, MagicMock

    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")

        # Mock a successful esphome config run.
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Configuration is valid!\n", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            resp = await ta.post("/ui/api/validate", json={"target": "device1.yaml"})
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert "valid" in data["output"].lower()

            # Verify esphome config was called with the correct target path.
            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert args[0] == "esphome"
            assert args[1] == "config"
            assert "device1.yaml" in str(args[2])

        # Also test a failed validation.
        mock_proc.communicate = AsyncMock(return_value=(b"ERROR: Invalid YAML\n", b""))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await ta.post("/ui/api/validate", json={"target": "device1.yaml"})
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is False
            assert "Invalid YAML" in data["output"]
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------

async def test_rename_target_updates_file_and_name(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "old_device.yaml", "old-device")
        resp = await ta.post(
            "/ui/api/targets/old_device.yaml/rename",
            json={"new_name": "new-device"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["new_filename"] == "new-device.yaml"

        new_path = ta.config_dir / "new-device.yaml"
        assert new_path.exists()
        assert not (ta.config_dir / "old_device.yaml").exists()
        content = new_path.read_text()
        assert "new-device" in content
    finally:
        await ta.close()


async def test_rename_target_missing_source(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post(
            "/ui/api/targets/ghost.yaml/rename",
            json={"new_name": "new"},
        )
        assert resp.status == 404
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# queue / retry / clear / remove / cancel
# ---------------------------------------------------------------------------

async def test_queue_returns_jobs(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        await ta.queue.enqueue("device1.yaml", "2024.3.1", "run1", 300)
        resp = await ta.get("/ui/api/queue")
        assert resp.status == 200
        jobs = await resp.json()
        assert len(jobs) == 1
        assert jobs[0]["target"] == "device1.yaml"
    finally:
        await ta.close()


async def test_retry_failed_job(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        job = await ta.queue.enqueue("device1.yaml", "2024.3.1", "run1", 300)
        claimed = await ta.queue.claim_next("client-A")
        await ta.queue.submit_result(claimed.id, "failed", log="error")

        resp = await ta.post("/ui/api/retry", json={"job_ids": [job.id]})
        assert resp.status == 200
        data = await resp.json()
        assert data["retried"] == 1

        # A new pending job should exist for the same target
        pending = [j for j in ta.queue.get_all() if j.state == JobState.PENDING]
        assert len(pending) == 1
        assert pending[0].target == "device1.yaml"
    finally:
        await ta.close()


async def test_retry_all_failed(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        _write_config(ta.config_dir, "b.yaml", "b")
        for target in ("a.yaml", "b.yaml"):
            await ta.queue.enqueue(target, "2024.3.1", "run1", 300)
            claimed = await ta.queue.claim_next("client-A")
            await ta.queue.submit_result(claimed.id, "failed", log="error")

        resp = await ta.post("/ui/api/retry", json={"job_ids": "all_failed"})
        data = await resp.json()
        assert data["retried"] == 2
    finally:
        await ta.close()


async def test_cancel_jobs(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        job = await ta.queue.enqueue("device1.yaml", "2024.3.1", "run1", 300)

        resp = await ta.post("/ui/api/cancel", json={"job_ids": [job.id]})
        assert resp.status == 200
        data = await resp.json()
        assert data["cancelled"] == 1
        assert ta.queue.get(job.id).state == JobState.CANCELLED
    finally:
        await ta.close()


async def test_queue_clear_by_state(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        await ta.queue.enqueue("device1.yaml", "2024.3.1", "run1", 300)
        claimed = await ta.queue.claim_next("client-A")
        await ta.queue.submit_result(claimed.id, "success", log="ok", ota_result="success")

        resp = await ta.post("/ui/api/queue/clear", json={"states": ["success"]})
        assert resp.status == 200
        data = await resp.json()
        assert data["cleared"] == 1
        assert ta.queue.queue_size() == 0
    finally:
        await ta.close()


async def test_queue_remove_by_id(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "device1.yaml", "device1")
        job = await ta.queue.enqueue("device1.yaml", "2024.3.1", "run1", 300)
        claimed = await ta.queue.claim_next("client-A")
        await ta.queue.submit_result(claimed.id, "failed", log="error")

        resp = await ta.post("/ui/api/queue/remove", json={"ids": [job.id]})
        assert resp.status == 200
        assert ta.queue.get(job.id) is None
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------

async def test_workers_lists_registered(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        ta.registry.register("worker-1", "linux/amd64", image_version="4")
        ta.registry.register("worker-2", "linux/arm64", image_version="4")
        resp = await ta.get("/ui/api/workers")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 2
        hostnames = {w["hostname"] for w in data}
        assert hostnames == {"worker-1", "worker-2"}
    finally:
        await ta.close()


async def test_worker_set_parallel_jobs(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("w", "linux/amd64", image_version="4")
        resp = await ta.post(
            f"/ui/api/workers/{client_id}/parallel-jobs",
            json={"max_parallel_jobs": 4},
        )
        assert resp.status == 200
        worker = ta.registry.get(client_id)
        assert worker.requested_max_parallel_jobs == 4
    finally:
        await ta.close()


async def test_worker_set_parallel_jobs_rejects_out_of_range(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("w", "linux/amd64", image_version="4")
        resp = await ta.post(
            f"/ui/api/workers/{client_id}/parallel-jobs",
            json={"max_parallel_jobs": 99},
        )
        assert resp.status == 400
    finally:
        await ta.close()


async def test_worker_set_parallel_jobs_unknown_worker(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post(
            "/ui/api/workers/unknown-id/parallel-jobs",
            json={"max_parallel_jobs": 2},
        )
        assert resp.status == 404
    finally:
        await ta.close()


async def test_worker_remove_offline(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        from datetime import datetime, timedelta, timezone
        client_id = ta.registry.register("w", "linux/amd64", image_version="4")
        # Backdate last_seen so the worker is considered offline
        ta.registry.get(client_id).last_seen = datetime.now(timezone.utc) - timedelta(minutes=5)

        resp = await ta.delete(f"/ui/api/workers/{client_id}")
        assert resp.status == 200
        assert ta.registry.get(client_id) is None
    finally:
        await ta.close()


async def test_worker_remove_online_refused(tmp_path):
    """Can't remove an online worker — must be marked offline first."""
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("w", "linux/amd64", image_version="4")
        resp = await ta.delete(f"/ui/api/workers/{client_id}")
        assert resp.status == 409
        # Worker is still in the registry
        assert ta.registry.get(client_id) is not None
    finally:
        await ta.close()


async def test_worker_clean_cache_sets_pending_flag(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("w", "linux/amd64", image_version="4")
        resp = await ta.post(f"/ui/api/workers/{client_id}/clean")
        assert resp.status == 200
        assert ta.registry.get(client_id).pending_clean is True
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# DQ.5 — POST /ui/api/workers/{id}/disk-quota + GET /ui/api/workers payload
# ---------------------------------------------------------------------------


async def test_worker_set_disk_quota_persists_override(tmp_path, _enable_socket):
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("qworker", "linux/amd64", image_version="4")
        resp = await ta.post(
            f"/ui/api/workers/{client_id}/disk-quota",
            json={"disk_quota_bytes": 5 * 1024 ** 3},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True, "disk_quota_bytes": 5 * 1024 ** 3}
        assert ta.registry.get(client_id).disk_quota_bytes == 5 * 1024 ** 3
        assert ta.app["worker_disk_quota_store"].get_quota("qworker") == 5 * 1024 ** 3
    finally:
        await ta.close()


async def test_worker_set_disk_quota_null_clears_override(tmp_path, _enable_socket):
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("qworker", "linux/amd64", image_version="4")
        # Seed an override.
        await ta.post(
            f"/ui/api/workers/{client_id}/disk-quota",
            json={"disk_quota_bytes": 5 * 1024 ** 3},
        )
        # Clear it.
        resp = await ta.post(
            f"/ui/api/workers/{client_id}/disk-quota",
            json={"disk_quota_bytes": None},
        )
        assert resp.status == 200
        assert ta.registry.get(client_id).disk_quota_bytes is None
        assert ta.app["worker_disk_quota_store"].get_quota("qworker") is None
    finally:
        await ta.close()


async def test_worker_set_disk_quota_rejects_below_floor(tmp_path, _enable_socket):
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("qworker", "linux/amd64", image_version="4")
        resp = await ta.post(
            f"/ui/api/workers/{client_id}/disk-quota",
            json={"disk_quota_bytes": 1024 ** 3 - 1},  # < 1 GiB
        )
        assert resp.status == 400
    finally:
        await ta.close()


async def test_worker_set_disk_quota_rejects_above_ceiling(tmp_path, _enable_socket):
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("qworker", "linux/amd64", image_version="4")
        resp = await ta.post(
            f"/ui/api/workers/{client_id}/disk-quota",
            json={"disk_quota_bytes": (1024 + 1) * 1024 ** 3},  # > 1 TiB
        )
        assert resp.status == 400
    finally:
        await ta.close()


async def test_worker_set_disk_quota_rejects_non_integer(tmp_path, _enable_socket):
    ta = await _make_ui_app(tmp_path)
    try:
        client_id = ta.registry.register("qworker", "linux/amd64", image_version="4")
        resp = await ta.post(
            f"/ui/api/workers/{client_id}/disk-quota",
            json={"disk_quota_bytes": "5GiB"},
        )
        assert resp.status == 400
    finally:
        await ta.close()


async def test_worker_set_disk_quota_unknown_worker(tmp_path, _enable_socket):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post(
            "/ui/api/workers/unknown-id/disk-quota",
            json={"disk_quota_bytes": 5 * 1024 ** 3},
        )
        assert resp.status == 404
    finally:
        await ta.close()


async def test_get_workers_includes_disk_quota_fields(tmp_path, _enable_socket):
    ta = await _make_ui_app(tmp_path)
    try:
        # Worker with no override → effective = fleet default (10 GiB).
        ta.registry.register("worker-default", "linux/amd64", image_version="4")
        # Worker with an override → effective = override.
        client_id = ta.registry.register("worker-override", "linux/amd64", image_version="4")
        ta.registry.set_disk_quota(client_id, 3 * 1024 ** 3)

        resp = await ta.get("/ui/api/workers")
        assert resp.status == 200
        rows = await resp.json()
        by_host = {r["hostname"]: r for r in rows}

        assert by_host["worker-default"]["disk_quota_override_bytes"] is None
        assert by_host["worker-default"]["disk_quota_bytes"] == 10 * 1024 ** 3
        assert by_host["worker-default"]["default_worker_disk_quota_bytes"] == 10 * 1024 ** 3

        assert by_host["worker-override"]["disk_quota_override_bytes"] == 3 * 1024 ** 3
        assert by_host["worker-override"]["disk_quota_bytes"] == 3 * 1024 ** 3
        assert by_host["worker-override"]["default_worker_disk_quota_bytes"] == 10 * 1024 ** 3
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# POST /ui/api/targets — CD.3 (create + duplicate device)
# ---------------------------------------------------------------------------


async def test_create_target_stub(tmp_path):
    """POST /ui/api/targets with no source creates a staged dotfile."""
    import yaml
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post("/ui/api/targets", json={"filename": "kitchen"})
        assert resp.status == 200
        data = await resp.json()
        # #62: create returns a .pending. prefixed filename
        assert data["target"] == ".pending.kitchen.yaml"
        # File is staged as a dotfile at the config root (not the final name)
        staged = ta.config_dir / ".pending.kitchen.yaml"
        assert staged.exists()
        parsed = yaml.safe_load(staged.read_text())
        assert parsed["esphome"]["name"] == "kitchen"
        # Final name does NOT exist yet (not written until first save)
        assert not (ta.config_dir / "kitchen.yaml").exists()
    finally:
        await ta.close()


async def test_create_target_accepts_yaml_extension(tmp_path):
    """filename='kitchen.yaml' is normalised and accepted."""
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post("/ui/api/targets", json={"filename": "kitchen.yaml"})
        assert resp.status == 200
        data = await resp.json()
        assert data["target"] == ".pending.kitchen.yaml"
    finally:
        await ta.close()


async def test_create_target_rejects_collision(tmp_path):
    """Creating a filename that already exists returns 400."""
    ta = await _make_ui_app(tmp_path)
    try:
        (ta.config_dir / "existing.yaml").write_text("esphome:\n  name: existing\n")
        resp = await ta.post("/ui/api/targets", json={"filename": "existing"})
        assert resp.status == 400
        body = await resp.json()
        assert "already exists" in body["error"]
    finally:
        await ta.close()


async def test_create_target_rejects_path_traversal(tmp_path):
    """A filename containing slashes is rejected by the slug regex."""
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post("/ui/api/targets", json={"filename": "../etc/passwd"})
        assert resp.status == 400
    finally:
        await ta.close()


async def test_create_target_rejects_invalid_slug(tmp_path):
    """Underscores, uppercase, spaces all rejected by the slug regex."""
    ta = await _make_ui_app(tmp_path)
    try:
        for bad in ("Kitchen", "my_device", "device 1", "-leading-hyphen", ""):
            resp = await ta.post("/ui/api/targets", json={"filename": bad})
            assert resp.status == 400, f"expected 400 for {bad!r}"
    finally:
        await ta.close()


async def test_create_target_duplicate(tmp_path):
    """POST /ui/api/targets with source duplicates and rewrites esphome.name."""
    import yaml
    ta = await _make_ui_app(tmp_path)
    try:
        (ta.config_dir / "source.yaml").write_text(
            "esphome:\n  name: original\n  comment: Hello\n"
            "wifi:\n  ssid: home\n"
        )
        resp = await ta.post(
            "/ui/api/targets",
            json={"filename": "copy", "source": "source.yaml"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["target"] == ".pending.copy.yaml"

        created = ta.config_dir / ".pending.copy.yaml"
        parsed = yaml.safe_load(created.read_text())
        assert parsed["esphome"]["name"] == "copy"
        assert parsed["esphome"]["comment"] == "Hello"
        assert parsed["wifi"]["ssid"] == "home"
    finally:
        await ta.close()


async def test_create_target_duplicate_missing_source(tmp_path):
    """Duplicating from a non-existent source returns 404."""
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post(
            "/ui/api/targets",
            json={"filename": "new", "source": "nonexistent.yaml"},
        )
        assert resp.status == 404
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# FD.2 / FD.6 — compile(download_only) + /ui/api/jobs/{id}/firmware download
# ---------------------------------------------------------------------------

async def test_compile_accepts_download_only_flag(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        resp = await ta.post(
            "/ui/api/compile",
            json={"targets": ["a.yaml"], "download_only": True},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["enqueued"] == 1
        job = ta.queue.get_all()[0]
        assert job.download_only is True
        assert job.has_firmware is False
    finally:
        await ta.close()


async def test_compile_defaults_download_only_to_false(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "a.yaml", "a")
        resp = await ta.post("/ui/api/compile", json={"targets": ["a.yaml"]})
        assert resp.status == 200
        job = ta.queue.get_all()[0]
        assert job.download_only is False
    finally:
        await ta.close()


async def test_firmware_download_streams_stored_bin(tmp_path, monkeypatch):
    import firmware_storage
    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    ta = await _make_ui_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="office.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        assert job is not None
        await ta.queue.claim_next("any")
        # #69: save via the default (factory) variant; the endpoint
        # picks the first variant reported by list_variants when no
        # ?variant= is given.
        firmware_storage.save_firmware(
            job.id, b"HELLO_FW", variant="factory", root=firmware_dir,
        )
        await ta.queue.mark_firmware_stored(job.id)

        resp = await ta.get(f"/ui/api/jobs/{job.id}/firmware")
        assert resp.status == 200
        body = await resp.read()
        assert body == b"HELLO_FW"
        # Must arrive as an attachment with a filename derived from the target + short id.
        cd = resp.headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert "office-" in cd
        # #69: non-legacy variants (factory/ota) are tagged in the filename.
        assert "-factory.bin" in cd
    finally:
        await ta.close()


async def test_firmware_download_selects_variant_by_query(tmp_path, monkeypatch):
    """#69 — ?variant=ota serves the OTA binary even when factory exists."""
    import firmware_storage
    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    ta = await _make_ui_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="office.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        await ta.queue.claim_next("any")
        firmware_storage.save_firmware(
            job.id, b"FACTORY", variant="factory", root=firmware_dir,
        )
        firmware_storage.save_firmware(
            job.id, b"OTA_ONLY", variant="ota", root=firmware_dir,
        )
        await ta.queue.mark_firmware_stored(job.id)

        resp = await ta.get(f"/ui/api/jobs/{job.id}/firmware?variant=ota")
        assert resp.status == 200
        assert await resp.read() == b"OTA_ONLY"
        assert "-ota.bin" in resp.headers["Content-Disposition"]

        resp = await ta.get(f"/ui/api/jobs/{job.id}/firmware?variant=factory")
        assert await resp.read() == b"FACTORY"
        assert "-factory.bin" in resp.headers["Content-Disposition"]

        # Unknown variant → 404 with the available list in the body.
        resp = await ta.get(f"/ui/api/jobs/{job.id}/firmware?variant=nope")
        assert resp.status == 404
        data = await resp.json()
        assert data["available"] == ["factory", "ota"]
    finally:
        await ta.close()


async def test_firmware_download_gzip_flag_compresses_body(tmp_path, monkeypatch):
    """#69 — ?gz=1 wraps the response in gzip and serves a .bin.gz filename."""
    import gzip
    import firmware_storage
    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    ta = await _make_ui_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="office.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        await ta.queue.claim_next("any")
        payload = b"A" * 4096  # compressible
        firmware_storage.save_firmware(
            job.id, payload, variant="factory", root=firmware_dir,
        )
        await ta.queue.mark_firmware_stored(job.id)

        resp = await ta.get(f"/ui/api/jobs/{job.id}/firmware?gz=1")
        assert resp.status == 200
        body = await resp.read()
        # Content-Encoding: identity ensures aiohttp didn't transparently
        # re-inflate on the client side.
        assert resp.headers["Content-Encoding"] == "identity"
        assert gzip.decompress(body) == payload
        # Compression is real — body should be materially smaller than
        # the uncompressed 4096 bytes (4096 A's compresses to <100).
        assert len(body) < 200
        assert resp.headers["Content-Disposition"].endswith('.bin.gz"')
    finally:
        await ta.close()


async def test_firmware_download_returns_404_when_unavailable(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="a.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        assert job is not None
        # has_firmware is False → endpoint returns 404.
        resp = await ta.get(f"/ui/api/jobs/{job.id}/firmware")
        assert resp.status == 404
    finally:
        await ta.close()


async def test_firmware_download_404_when_job_missing(tmp_path):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/jobs/ghost/firmware")
        assert resp.status == 404
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# settings (SP.3)
# ---------------------------------------------------------------------------

@pytest.fixture
def _settings_init(tmp_path):
    """Redirect the settings module to a scratch dir for each test."""
    import settings as settings_mod
    settings_mod._reset_for_tests()
    settings_mod.init_settings(
        settings_path=tmp_path / "settings.json",
        options_path=tmp_path / "options.json",
    )
    yield
    settings_mod._reset_for_tests()


async def test_get_settings_returns_defaults_on_fresh_boot(tmp_path, _settings_init):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/settings")
        assert resp.status == 200
        data = await resp.json()
        # Token is auto-generated + test harness sets "ui-test-token"
        # (see _make_ui_app); assert on shape, not the value.
        assert isinstance(data.pop("server_token"), str)
        assert data == {
            # _make_ui_app PATCHes 'on' so the file-history endpoints
            # in this module's other tests work; that's what GET sees
            # here. The dataclass default ('unset' on a truly-fresh
            # boot) is covered by tests/test_settings.py.
            "versioning_enabled": "on",
            "auto_commit_on_save": True,
            "git_author_name": "HA User",
            "git_author_email": "ha@distributed-esphome.local",
            "job_history_retention_days": 365,
            "firmware_cache_max_gb": 2.0,
            "firmware_retention_days": 2,
            "job_log_retention_days": 30,
            "job_timeout": 600,
            "ota_timeout": 120,
            "worker_offline_threshold": 30,
            "device_poll_interval": 60,
            "require_ha_auth": False,
            "time_format": "auto",
            "date_format": "auto",
            "default_worker_disk_quota_bytes": 10 * 1024 ** 3,
        }
    finally:
        await ta.close()


async def test_patch_settings_updates_and_returns_full_blob(tmp_path, _settings_init):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.client.patch(
            "/ui/api/settings",
            json={"auto_commit_on_save": False, "job_history_retention_days": 90},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["auto_commit_on_save"] is False
        assert data["job_history_retention_days"] == 90
        # Unspecified fields preserved.
        assert data["firmware_cache_max_gb"] == 2.0
    finally:
        await ta.close()


async def test_patch_settings_rejects_unknown_key(tmp_path, _settings_init):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.client.patch(
            "/ui/api/settings",
            json={"bogus": 1},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["field"] == "bogus"
    finally:
        await ta.close()


async def test_patch_settings_rejects_out_of_range(tmp_path, _settings_init):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.client.patch(
            "/ui/api/settings",
            json={"firmware_cache_max_gb": 0.0},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["field"] == "firmware_cache_max_gb"
    finally:
        await ta.close()


async def test_patch_settings_rejects_non_json_body(tmp_path, _settings_init):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.client.patch(
            "/ui/api/settings",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
    finally:
        await ta.close()


async def test_patch_settings_rejects_non_object_body(tmp_path, _settings_init):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.client.patch(
            "/ui/api/settings",
            json=[1, 2, 3],
        )
        assert resp.status == 400
    finally:
        await ta.close()


async def test_patch_settings_persists_across_get(tmp_path, _settings_init):
    """Live-effect floor: a PATCH is immediately observable via GET."""
    ta = await _make_ui_app(tmp_path)
    try:
        patch_resp = await ta.client.patch(
            "/ui/api/settings",
            json={"auto_commit_on_save": False},
        )
        assert patch_resp.status == 200

        get_resp = await ta.get("/ui/api/settings")
        data = await get_resp.json()
        assert data["auto_commit_on_save"] is False
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# auto-versioning (AV.1 / AV.2)
# ---------------------------------------------------------------------------

async def test_editor_save_triggers_auto_commit(tmp_path, _settings_init):
    """AV.2: save via POST /ui/api/targets/{f}/content produces a git commit."""
    import subprocess

    import git_versioning as gv
    gv._reset_for_tests()

    ta = await _make_ui_app(tmp_path)
    try:
        # Seed a config file and init the repo under the test config dir.
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        # Short debounce so the test doesn't stall.
        old = gv.DEBOUNCE_SECONDS
        gv.DEBOUNCE_SECONDS = 0.05
        try:
            resp = await ta.post(
                "/ui/api/targets/bedroom.yaml/content",
                json={"content": "esphome:\n  name: bedroom\n# edited\n"},
            )
            assert resp.status == 200
            await gv.drain_pending_commits()
        finally:
            gv.DEBOUNCE_SECONDS = old

        log = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=str(ta.config_dir),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        # Bug #34: auto-save subject is the human-readable form.
        assert "Automatically saved after editing in UI" in log
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_editor_save_with_skip_commit_writes_file_but_no_commit(
    tmp_path, _settings_init,
):
    """Bug #136 follow-up: ``skip_commit: true`` writes the file but
    leaves the commit step to the explicit Save & Close path."""
    import subprocess

    import git_versioning as gv
    gv._reset_for_tests()

    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        # Snapshot current commit count so we can assert no NEW commit lands.
        baseline = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(ta.config_dir), capture_output=True, text=True, check=True,
        ).stdout.strip()

        old = gv.DEBOUNCE_SECONDS
        gv.DEBOUNCE_SECONDS = 0.05
        try:
            resp = await ta.post(
                "/ui/api/targets/bedroom.yaml/content",
                json={
                    "content": "esphome:\n  name: bedroom\n# edited via plain Save\n",
                    "skip_commit": True,
                },
            )
            assert resp.status == 200
            await gv.drain_pending_commits()
        finally:
            gv.DEBOUNCE_SECONDS = old

        # File written.
        assert (
            "edited via plain Save"
            in (ta.config_dir / "bedroom.yaml").read_text()
        )

        # No new commit landed — the commit count is unchanged from baseline.
        after = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(ta.config_dir), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert after == baseline, (
            f"skip_commit=true must not produce a commit (was {baseline}, now {after})"
        )
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_file_history_endpoint_returns_entries(tmp_path, _settings_init):
    """AV.3: GET /ui/api/files/{f}/history returns the file's commit log."""
    import subprocess
    import git_versioning as gv
    gv._reset_for_tests()

    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        # Make an edit via the editor endpoint so we get a commit.
        old = gv.DEBOUNCE_SECONDS
        gv.DEBOUNCE_SECONDS = 0.05
        try:
            resp = await ta.post(
                "/ui/api/targets/bedroom.yaml/content",
                json={"content": "esphome:\n  name: bedroom\n# edit 1\n"},
            )
            assert resp.status == 200
            await gv.drain_pending_commits()
        finally:
            gv.DEBOUNCE_SECONDS = old

        resp = await ta.get("/ui/api/files/bedroom.yaml/history")
        assert resp.status == 200
        entries = await resp.json()
        assert isinstance(entries, list)
        assert len(entries) >= 1
        # Newest entry should be our save. Bug #34: human-readable subject.
        assert entries[0]["message"] == "Automatically saved after editing in UI"
        assert "hash" in entries[0]
        assert "short_hash" in entries[0]
        assert isinstance(entries[0]["date"], int)
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_file_history_endpoint_rejects_invalid_pagination(tmp_path, _settings_init):
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/files/bedroom.yaml/history?limit=nope")
        assert resp.status == 400
    finally:
        await ta.close()


async def test_file_status_endpoint(tmp_path, _settings_init):
    """AV.6: GET /files/{f}/status reports dirty state + HEAD hash."""
    import git_versioning as gv
    gv._reset_for_tests()
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        # Clean tree first.
        resp = await ta.get("/ui/api/files/bedroom.yaml/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["has_uncommitted_changes"] is False
        assert data["head_hash"]

        # Dirty the tree without going through a committing endpoint.
        (ta.config_dir / "bedroom.yaml").write_text("esphome:\n  name: bedroom\n# external-edit\n")
        resp = await ta.get("/ui/api/files/bedroom.yaml/status")
        data = await resp.json()
        assert data["has_uncommitted_changes"] is True
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_file_rollback_endpoint_restores_and_commits(tmp_path, _settings_init):
    """AV.5: rollback endpoint restores file content and records a revert."""
    import git_versioning as gv
    gv._reset_for_tests()
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        # Create two more versions via editor saves.
        old = gv.DEBOUNCE_SECONDS
        gv.DEBOUNCE_SECONDS = 0.05
        try:
            await ta.post(
                "/ui/api/targets/bedroom.yaml/content",
                json={"content": "esphome:\n  name: bedroom\n# v2\n"},
            )
            await gv.drain_pending_commits()
            await ta.post(
                "/ui/api/targets/bedroom.yaml/content",
                json={"content": "esphome:\n  name: bedroom\n# v3\n"},
            )
            await gv.drain_pending_commits()
        finally:
            gv.DEBOUNCE_SECONDS = old

        hist = await (await ta.get("/ui/api/files/bedroom.yaml/history")).json()
        target_hash = hist[1]["hash"]  # the v2 commit

        resp = await ta.post(
            "/ui/api/files/bedroom.yaml/rollback",
            json={"hash": target_hash},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["committed"] is True
        assert "# v2" in data["content"]
        assert "# v3" not in data["content"]

        # File on disk was updated.
        assert (ta.config_dir / "bedroom.yaml").read_text() == "esphome:\n  name: bedroom\n# v2\n"
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_file_rollback_endpoint_rejects_missing_hash(tmp_path, _settings_init):
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        resp = await ta.post("/ui/api/files/bedroom.yaml/rollback", json={})
        assert resp.status == 400
    finally:
        await ta.close()


async def test_file_commit_endpoint_creates_commit(tmp_path, _settings_init):
    """AV.11: manual commit endpoint works even with auto-commit off."""
    import git_versioning as gv
    gv._reset_for_tests()
    # Flip auto-commit off to mimic the Pat-with-git scenario.
    from settings import update_settings
    await update_settings({"auto_commit_on_save": False})

    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        # Save via editor — no commit happens because auto-off.
        await ta.post(
            "/ui/api/targets/bedroom.yaml/content",
            json={"content": "esphome:\n  name: bedroom\n# manually-committed\n"},
        )

        # Manual commit: no message → default marker.
        resp = await ta.post("/ui/api/files/bedroom.yaml/commit", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["committed"] is True
        assert data["hash"]
        # Bug #34: manual-commit default subject is human-readable.
        assert data["message"] == "Manually committed from UI"

        # Re-committing without changes is a no-op.
        resp = await ta.post("/ui/api/files/bedroom.yaml/commit", json={})
        data = await resp.json()
        assert data["committed"] is False
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_file_commit_endpoint_respects_custom_message(tmp_path, _settings_init):
    import git_versioning as gv
    gv._reset_for_tests()
    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)
        (ta.config_dir / "bedroom.yaml").write_text("external edit\n")

        resp = await ta.post(
            "/ui/api/files/bedroom.yaml/commit",
            json={"message": "captured external edit"},
        )
        data = await resp.json()
        assert data["committed"] is True
        assert data["message"] == "captured external edit"
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_file_diff_endpoint_returns_unified_diff(tmp_path, _settings_init):
    """AV.4: GET /ui/api/files/{f}/diff returns a unified diff between two commits."""
    import git_versioning as gv
    gv._reset_for_tests()

    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        old = gv.DEBOUNCE_SECONDS
        gv.DEBOUNCE_SECONDS = 0.05
        try:
            await ta.post(
                "/ui/api/targets/bedroom.yaml/content",
                json={"content": "esphome:\n  name: bedroom\n# v2\n"},
            )
            await gv.drain_pending_commits()
            await ta.post(
                "/ui/api/targets/bedroom.yaml/content",
                json={"content": "esphome:\n  name: bedroom\n# v3\n"},
            )
            await gv.drain_pending_commits()
        finally:
            gv.DEBOUNCE_SECONDS = old

        hist_resp = await ta.get("/ui/api/files/bedroom.yaml/history")
        entries = await hist_resp.json()
        newer = entries[0]["hash"]
        older = entries[1]["hash"]

        diff_resp = await ta.get(f"/ui/api/files/bedroom.yaml/diff?from={older}&to={newer}")
        assert diff_resp.status == 200
        body = await diff_resp.json()
        assert "diff" in body
        assert "-# v2" in body["diff"]
        assert "+# v3" in body["diff"]
    finally:
        gv._reset_for_tests()
        await ta.close()


async def test_editor_save_skips_commit_when_toggle_off(tmp_path, _settings_init):
    """AV.2: turning off auto_commit_on_save disables the git interaction."""
    import subprocess

    import git_versioning as gv
    gv._reset_for_tests()

    # Flip the toggle before the save.
    from settings import update_settings
    await update_settings({"auto_commit_on_save": False})

    ta = await _make_ui_app(tmp_path)
    try:
        _write_config(ta.config_dir, "bedroom.yaml", "bedroom")
        gv.init_repo(ta.config_dir)

        baseline_log = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=str(ta.config_dir),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()

        old = gv.DEBOUNCE_SECONDS
        gv.DEBOUNCE_SECONDS = 0.05
        try:
            resp = await ta.post(
                "/ui/api/targets/bedroom.yaml/content",
                json={"content": "esphome:\n  name: bedroom\n# edited-but-no-commit\n"},
            )
            assert resp.status == 200
            await gv.drain_pending_commits()
        finally:
            gv.DEBOUNCE_SECONDS = old

        after_log = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=str(ta.config_dir),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert after_log == baseline_log
    finally:
        gv._reset_for_tests()
        await ta.close()


# ---------------------------------------------------------------------------
# POST /ui/api/esphome-version — bug #105
# ---------------------------------------------------------------------------

async def test_set_esphome_version_schedules_install(tmp_path):
    """Picking a version in the UI must schedule the install, not just
    record the selection. Previously this handler only updated the
    in-memory selected version; on a fresh HAOS box with no bundled
    ESPHome the user had no way to unblock the "Installing ESPHome…"
    banner (bug #105)."""
    ta = await _make_ui_app(tmp_path)
    try:
        scheduled: list[str] = []

        def fake_ensure(ver: str) -> None:
            scheduled.append(ver)

        import scanner as scanner_module
        with (
            patch.object(scanner_module, "ensure_esphome_installed", fake_ensure),
            patch.object(scanner_module, "set_esphome_version", lambda v: None),
        ):
            resp = await ta.post(
                "/ui/api/esphome-version",
                json={"version": "2026.3.3"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "version": "2026.3.3"}

        # Give the executor a tick to run.
        import asyncio
        for _ in range(50):
            if scheduled:
                break
            await asyncio.sleep(0.01)

        assert scheduled == ["2026.3.3"], (
            "POST /ui/api/esphome-version did not schedule ensure_esphome_installed — "
            "this is the #105 UI-recovery path"
        )
    finally:
        await ta.close()


async def test_set_esphome_version_rejects_missing_version(tmp_path):
    """Empty body or missing version returns 400."""
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post("/ui/api/esphome-version", json={})
        assert resp.status == 400
        resp = await ta.post("/ui/api/esphome-version", json={"version": ""})
        assert resp.status == 400
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# DM.2: ICMP ping diagnostic
# ---------------------------------------------------------------------------


async def test_ping_returns_503_when_no_device_poller(tmp_path, _enable_socket):
    """Without a device_poller hung off the app, ping has no way to find devices."""
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.post("/ui/api/targets/device1.yaml/ping")
        assert resp.status == 503
    finally:
        await ta.close()


async def test_ping_returns_404_for_unknown_target(tmp_path, _enable_socket):
    """A YAML the poller has never seen returns ``unknown_target``."""
    from datetime import datetime
    from device_poller import DevicePoller

    ta = await _make_ui_app(tmp_path)
    try:
        poller = DevicePoller()
        ta.client.server.app._state["device_poller"] = poller
        resp = await ta.post("/ui/api/targets/missing.yaml/ping")
        assert resp.status == 404
        body = await resp.json()
        assert body["error"] == "unknown_target"
        assert body["target"] == "missing.yaml"
        # Silence the unused-import warning for the date stub used elsewhere.
        _ = datetime.now()
    finally:
        await ta.close()


async def test_ping_returns_404_when_no_resolved_address(tmp_path, _enable_socket):
    """Device exists but resolve_ota_address returned None."""
    from datetime import datetime
    from device_poller import Device, DevicePoller

    ta = await _make_ui_app(tmp_path)
    try:
        poller = DevicePoller()
        # ip_address=None and no override → resolve_ota_address returns None.
        poller._devices["device1"] = Device(
            name="device1",
            ip_address=None,
            online=False,
            last_seen=datetime.now(),
            compile_target="device1.yaml",
        )
        ta.client.server.app._state["device_poller"] = poller

        resp = await ta.post("/ui/api/targets/device1.yaml/ping")
        assert resp.status == 404
        body = await resp.json()
        assert body["error"] == "no_resolved_address"
        assert body["target"] == "device1.yaml"
        assert body["device_name"] == "device1"
    finally:
        await ta.close()


async def test_ping_success_returns_stats(tmp_path, monkeypatch, _enable_socket):
    """Happy path: poller has the device, async_ping returns alive host."""
    from datetime import datetime
    from device_poller import Device, DevicePoller

    ta = await _make_ui_app(tmp_path)
    try:
        poller = DevicePoller()
        poller._devices["device1"] = Device(
            name="device1",
            ip_address="192.168.1.42",
            online=True,
            last_seen=datetime.now(),
            compile_target="device1.yaml",
        )
        ta.client.server.app._state["device_poller"] = poller

        # Stub icmplib.async_ping with a fake Host object whose attrs match
        # the real shape so the handler's coercions stay honest.
        class _FakeHost:
            is_alive = True
            packets_sent = 10
            packets_received = 9
            packet_loss = 0.1
            min_rtt = 1.2
            avg_rtt = 2.5
            max_rtt = 10.0
            jitter = 0.8

        async def _fake_ping(*args, **kwargs):
            return _FakeHost()

        import icmplib
        monkeypatch.setattr(icmplib, "async_ping", _fake_ping)

        resp = await ta.post("/ui/api/targets/device1.yaml/ping")
        assert resp.status == 200
        body = await resp.json()
        assert body["target"] == "device1.yaml"
        assert body["address"] == "192.168.1.42"
        assert body["is_alive"] is True
        assert body["packets_sent"] == 10
        assert body["packets_received"] == 9
        assert body["packet_loss"] == 0.1
        assert body["min_rtt"] == 1.2
        assert body["avg_rtt"] == 2.5
        assert body["max_rtt"] == 10.0
        assert body["jitter"] == 0.8
        assert isinstance(body["ran_at"], (int, float))
    finally:
        await ta.close()


async def test_ping_returns_500_when_icmplib_raises(tmp_path, monkeypatch, _enable_socket):
    """Network/permission failure in icmplib surfaces as 500 ``ping_failed``."""
    from datetime import datetime
    from device_poller import Device, DevicePoller

    ta = await _make_ui_app(tmp_path)
    try:
        poller = DevicePoller()
        poller._devices["device1"] = Device(
            name="device1",
            ip_address="192.168.1.42",
            online=True,
            last_seen=datetime.now(),
            compile_target="device1.yaml",
        )
        ta.client.server.app._state["device_poller"] = poller

        async def _boom(*args, **kwargs):
            raise OSError("permission denied (no CAP_NET_RAW)")

        import icmplib
        monkeypatch.setattr(icmplib, "async_ping", _boom)

        resp = await ta.post("/ui/api/targets/device1.yaml/ping")
        assert resp.status == 500
        body = await resp.json()
        assert body["error"] == "ping_failed"
        assert "permission denied" in body["detail"]
        assert body["address"] == "192.168.1.42"
    finally:
        await ta.close()
