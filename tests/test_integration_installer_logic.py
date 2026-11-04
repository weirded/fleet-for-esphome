"""Unit tests for the HA custom-integration auto-installer (HI.8)."""

from __future__ import annotations

import json
from pathlib import Path

from integration_installer import install_integration


def _write_manifest(directory: Path, version: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps({"domain": "esphome_fleet", "name": "Fleet for ESPHome", "version": version})
    )
    (directory / "__init__.py").write_text("# stub\n")


def test_skips_when_source_missing(tmp_path: Path) -> None:
    dest = tmp_path / "config" / "custom_components" / "esphome_fleet"
    result = install_integration(
        source_dir=tmp_path / "does_not_exist",
        destination_dir=dest,
    )
    assert result == "skipped_no_source"
    assert not dest.exists()


def test_fresh_install_copies_all_files(tmp_path: Path) -> None:
    src = tmp_path / "src" / "esphome_fleet"
    _write_manifest(src, "0.1.0")
    (src / "sensor.py").write_text("# stub sensor\n")

    dest = tmp_path / "config" / "custom_components" / "esphome_fleet"
    result = install_integration(source_dir=src, destination_dir=dest)

    assert result == "installed"
    assert (dest / "manifest.json").exists()
    assert (dest / "__init__.py").exists()
    assert (dest / "sensor.py").exists()
    assert json.loads((dest / "manifest.json").read_text())["version"] == "0.1.0"


def test_unchanged_when_versions_match(tmp_path: Path) -> None:
    src = tmp_path / "src" / "esphome_fleet"
    _write_manifest(src, "0.2.0")
    dest = tmp_path / "config" / "custom_components" / "esphome_fleet"
    _write_manifest(dest, "0.2.0")
    # Mark the destination so we can detect a rewrite.
    (dest / "marker.txt").write_text("preserved")

    result = install_integration(source_dir=src, destination_dir=dest)
    assert result == "unchanged"
    # Marker file not wiped → destination wasn't touched.
    assert (dest / "marker.txt").read_text() == "preserved"


def test_update_replaces_old_version(tmp_path: Path) -> None:
    src = tmp_path / "src" / "esphome_fleet"
    _write_manifest(src, "0.3.0")
    (src / "new_file.py").write_text("# added in 0.3.0\n")

    dest = tmp_path / "config" / "custom_components" / "esphome_fleet"
    _write_manifest(dest, "0.2.0")
    (dest / "marker.txt").write_text("should-be-gone")

    result = install_integration(source_dir=src, destination_dir=dest)
    assert result == "updated"
    assert json.loads((dest / "manifest.json").read_text())["version"] == "0.3.0"
    assert (dest / "new_file.py").exists()
    # Old files cleared in the replacement.
    assert not (dest / "marker.txt").exists()


def test_failed_when_source_manifest_unparseable(tmp_path: Path) -> None:
    src = tmp_path / "src" / "esphome_fleet"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text("not valid json {{{")

    dest = tmp_path / "config" / "custom_components" / "esphome_fleet"
    # Point version_file at a non-existent path so we exercise the
    # manifest-parse fallback without picking up the host /app/VERSION.
    result = install_integration(
        source_dir=src,
        destination_dir=dest,
        version_file=tmp_path / "nonexistent_VERSION",
    )
    assert result == "failed"
    assert not dest.exists()


def test_version_file_overrides_manifest_version(tmp_path: Path) -> None:
    """#30 — manifest version in the destination matches /app/VERSION."""
    src = tmp_path / "src" / "esphome_fleet"
    _write_manifest(src, "0.1.0")  # stale placeholder in source

    version_file = tmp_path / "VERSION"
    version_file.write_text("1.4.1-dev.46\n")

    dest = tmp_path / "config" / "custom_components" / "esphome_fleet"
    result = install_integration(
        source_dir=src, destination_dir=dest, version_file=version_file
    )

    assert result == "installed"
    installed = json.loads((dest / "manifest.json").read_text())
    assert installed["version"] == "1.4.1-dev.46"


# ---------------------------------------------------------------------------
# #257: restart notice after an integration update
# ---------------------------------------------------------------------------

import pytest  # noqa: E402 — appended section keeps its own imports together
from aiohttp import web  # noqa: E402 — same
from aiohttp.test_utils import TestServer  # noqa: E402 — same

from integration_installer import (  # noqa: E402 — same
    RESTART_NOTIFICATION_ID,
    notify_restart_required,
)


@pytest.fixture(autouse=True)
def _enable_socket():
    """Loopback for TestServer; pytest-socket blocks it on some dev boxes (see test_api.py)."""
    try:
        import pytest_socket as _pytest_socket  # type: ignore[import-not-found]  # optional dev-only plugin
    except ImportError:
        yield
        return
    _pytest_socket.enable_socket()
    yield


async def _fake_ha(components: list[str], *, service_status: int = 200):
    """Tiny stand-in for the Supervisor core-API proxy; records POSTs."""
    posted: list[dict] = []
    seen_auth: list[str] = []

    async def config(request: web.Request) -> web.Response:
        seen_auth.append(request.headers.get("Authorization", ""))
        return web.json_response({"components": components})

    async def create(request: web.Request) -> web.Response:
        posted.append(await request.json())
        return web.json_response([], status=service_status)

    app = web.Application()
    app.router.add_get("/core/api/config", config)
    app.router.add_post("/core/api/services/persistent_notification/create", create)
    server = TestServer(app)
    await server.start_server()
    return server, posted, seen_auth


async def test_restart_notice_posted_when_integration_loaded() -> None:
    server, posted, seen_auth = await _fake_ha(["sensor", "esphome_fleet"])
    try:
        ok = await notify_restart_required(
            "1.8.1", token="tok", base_url=str(server.make_url("/core/api")),
        )
    finally:
        await server.close()
    assert ok is True
    assert seen_auth == ["Bearer tok"]
    assert len(posted) == 1
    assert posted[0]["notification_id"] == RESTART_NOTIFICATION_ID
    assert "1.8.1" in posted[0]["message"]
    assert "Restart" in posted[0]["title"]


async def test_no_restart_notice_when_integration_not_loaded() -> None:
    """Not set up in HA (or HA still booting) → nothing to restart for."""
    server, posted, _ = await _fake_ha(["sensor", "esphome"])
    try:
        ok = await notify_restart_required(
            "1.8.1", token="tok", base_url=str(server.make_url("/core/api")),
        )
    finally:
        await server.close()
    assert ok is False
    assert posted == []


async def test_no_restart_notice_without_supervisor_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standalone Docker has no Supervisor → skip without any network call."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert await notify_restart_required("1.8.1", base_url="http://127.0.0.1:9") is False


async def test_restart_notice_never_raises_when_ha_unreachable() -> None:
    ok = await notify_restart_required(
        "1.8.1", token="tok", base_url="http://127.0.0.1:9/core/api", timeout_s=1.0,
    )
    assert ok is False


async def test_restart_notice_reports_failure_on_service_error() -> None:
    server, posted, _ = await _fake_ha(["esphome_fleet"], service_status=500)
    try:
        ok = await notify_restart_required(
            "1.8.1", token="tok", base_url=str(server.make_url("/core/api")),
        )
    finally:
        await server.close()
    assert ok is False
    assert len(posted) == 1
