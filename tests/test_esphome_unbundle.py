"""Server ESPHome unbundling tests (SE.9 partial — SE.2 / SE.3 / SE.7).

Covers the lazy-install path that SE.1 (dropping esphome from
requirements.txt) will eventually depend on. These tests exercise the
`scanner.ensure_esphome_installed` / `_activate_esphome_venv` /
`_get_installed_esphome_version` contract without hitting the real
network — `VersionManager.ensure_version` is stubbed so the test is
hermetic.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import scanner


@pytest.fixture(autouse=True)
def _reset_scanner_state():
    """SE.2 module globals need a clean slate per test."""
    scanner._server_esphome_venv = None
    scanner._server_esphome_bin = None
    scanner._esphome_ready.clear()
    scanner._esphome_install_failed = False
    scanner._esphome_version_cache = None
    yield
    scanner._server_esphome_venv = None
    scanner._server_esphome_bin = None
    scanner._esphome_ready.clear()
    scanner._esphome_install_failed = False
    scanner._esphome_version_cache = None


def _make_fake_venv(tmp_path: Path, version: str) -> Path:
    """Build a minimal venv layout matching what VersionManager produces.

    The directory needs only the two things SE.3 looks at:
      - `<venv>/bin/esphome` — the binary path cached in `_server_esphome_bin`
      - `<venv>/lib/python{M.N}/site-packages/` — prepended to sys.path
    """
    venv = tmp_path / version
    site = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True)
    bin_dir = venv / "bin"
    bin_dir.mkdir()
    esphome_bin = bin_dir / "esphome"
    # Emit a realistic `esphome version` response so SE.7 parsing is exercised.
    esphome_bin.write_text(f"#!/bin/sh\necho 'Version: {version}'\n")
    esphome_bin.chmod(0o755)
    return venv


def test_activate_venv_prepends_site_packages(tmp_path: Path) -> None:
    """SE.3 — activating a venv puts its site-packages on sys.path."""
    venv = _make_fake_venv(tmp_path, "2026.4.0")
    site = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"

    original_sys_path = list(sys.path)
    try:
        assert scanner._activate_esphome_venv(venv) is True
        assert str(site) in sys.path
        # First entry — ensures lookups prefer the venv copy over any
        # bundled package already on sys.path.
        assert sys.path[0] == str(site)
    finally:
        sys.path[:] = original_sys_path


def test_activate_venv_is_idempotent(tmp_path: Path) -> None:
    """SE.3 — re-activating the same venv doesn't double up sys.path entries."""
    venv = _make_fake_venv(tmp_path, "2026.4.0")
    original_sys_path = list(sys.path)
    try:
        scanner._activate_esphome_venv(venv)
        before = list(sys.path)
        scanner._activate_esphome_venv(venv)
        assert sys.path == before
    finally:
        sys.path[:] = original_sys_path


def test_activate_venv_returns_false_on_missing_site_packages(tmp_path: Path) -> None:
    """SE.3 — malformed venv (no site-packages) is reported, not raised."""
    venv = tmp_path / "bad"
    venv.mkdir()
    assert scanner._activate_esphome_venv(venv) is False


def test_ensure_esphome_installed_happy_path(tmp_path: Path) -> None:
    """SE.2 — successful install wires module globals + fires ready event."""
    venv = _make_fake_venv(tmp_path, "2026.4.0")
    bin_path = str(venv / "bin" / "esphome")

    class _FakeVM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def ensure_version(self, version):
            assert version == "2026.4.0"
            return bin_path

    # Real sentinel writer so we can assert the server publishes its
    # active venv for the shared-dir local worker (#119 round 2).
    from version_manager import (
        SERVER_ACTIVE_VERSION_FILE,
        write_server_active_version,
    )

    original_sys_path = list(sys.path)
    try:
        with patch.object(scanner, "_activate_esphome_venv", wraps=scanner._activate_esphome_venv) as activate:
            with patch.dict(sys.modules, {"version_manager": type(sys)("version_manager")}):
                sys.modules["version_manager"].VersionManager = _FakeVM
                sys.modules["version_manager"].write_server_active_version = (
                    write_server_active_version
                )
                scanner.ensure_esphome_installed("2026.4.0", versions_base=tmp_path)

        assert scanner._esphome_ready.is_set()
        assert scanner._esphome_install_failed is False
        assert scanner._server_esphome_bin == bin_path
        assert scanner._server_esphome_venv == venv
        activate.assert_called_once_with(venv)
        # #119 (round 2): the active version is published so the local
        # worker's eviction won't delete the server's bundling venv.
        sentinel = tmp_path / SERVER_ACTIVE_VERSION_FILE
        assert sentinel.read_text().strip() == "2026.4.0"
    finally:
        sys.path[:] = original_sys_path


def test_ensure_esphome_installed_survives_vm_exception(tmp_path: Path) -> None:
    """SE.2 — VersionManager failure flips `_esphome_install_failed` but
    never raises out of the function. Ready event stays clear.
    """
    class _FakeVM:
        def __init__(self, **kwargs):
            pass
        def ensure_version(self, version):
            raise RuntimeError("network error")

    with patch.dict(sys.modules, {"version_manager": type(sys)("version_manager")}):
        sys.modules["version_manager"].VersionManager = _FakeVM
        scanner.ensure_esphome_installed("2026.4.0", versions_base=tmp_path)

    assert not scanner._esphome_ready.is_set()
    assert scanner._esphome_install_failed is True
    assert scanner._server_esphome_bin is None


def test_get_version_falls_back_to_importlib_when_venv_not_ready() -> None:
    """SE.7 — pre-install state uses the bundled package (test harness)."""
    # scanner module globals already reset by the autouse fixture.
    # importlib.metadata.version("esphome") works in CI (bundled pkg).
    version = scanner._get_installed_esphome_version()
    # Accept either a real version string or the test sentinel; the
    # point is that we don't crash or hang looking for a venv binary.
    assert version != "unknown"


def test_get_version_uses_venv_binary_when_ready(tmp_path: Path) -> None:
    """SE.7 — once the venv is ready, we shell out to `<venv>/bin/esphome
    version` and cache the result.
    """
    venv = _make_fake_venv(tmp_path, "2026.4.0")
    scanner._server_esphome_bin = str(venv / "bin" / "esphome")
    scanner._server_esphome_venv = venv
    scanner._esphome_ready.set()

    # The fake binary's stdout is `Version: 2026.4.0`. Patch subprocess.run
    # to return that canned output without spawning a process.
    canned = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Version: 2026.4.0\n", stderr="",
    )
    with patch.object(scanner.subprocess, "run", return_value=canned) as run:
        assert scanner._get_installed_esphome_version() == "2026.4.0"
    run.assert_called_once()
    # Second call reads from cache — no new subprocess.
    with patch.object(scanner.subprocess, "run") as run:
        assert scanner._get_installed_esphome_version() == "2026.4.0"
    run.assert_not_called()


def test_set_esphome_version_busts_the_version_cache() -> None:
    """SE.7 — changing the selected version clears the memoized CLI probe."""
    scanner._esphome_version_cache = "2026.3.3"
    scanner.set_esphome_version("2026.4.0")
    assert scanner._esphome_version_cache is None


def test_get_version_returns_installing_during_install() -> None:
    """SE.7 — mid-flight install, no bundled package → "installing"."""
    # Simulate no bundled package: patch importlib.metadata.version to raise.
    with patch("importlib.metadata.version", side_effect=Exception("no pkg")):
        assert scanner._get_installed_esphome_version() == "installing"


def test_get_version_returns_unknown_on_hard_failure() -> None:
    """SE.7 — if install has failed AND no bundled package, surface "unknown"."""
    scanner._esphome_install_failed = True
    with patch("importlib.metadata.version", side_effect=Exception("no pkg")):
        assert scanner._get_installed_esphome_version() == "unknown"


# --- SE.4 — _resolve_esphome_config degradation ---


def test_resolve_esphome_config_returns_none_when_venv_not_ready_and_no_bundle(
    tmp_path: Path,
) -> None:
    """SE.4 — mid-install + no bundled package → clean None + INFO log."""
    yaml_file = tmp_path / "foo.yaml"
    yaml_file.write_text("esphome:\n  name: foo\n")
    # scanner._esphome_ready cleared by autouse fixture.
    # Simulate "no bundled esphome" via import hook.
    import builtins as _builtins
    real_import = _builtins.__import__

    def _no_esphome(name, *args, **kwargs):
        if name == "esphome" or name.startswith("esphome."):
            raise ImportError("no bundled esphome")
        return real_import(name, *args, **kwargs)

    with patch.object(_builtins, "__import__", side_effect=_no_esphome):
        result = scanner._resolve_esphome_config(str(tmp_path), "foo.yaml")
    assert result is None


# --- SE.8 — server-info install status + reinstall endpoint ---


async def test_server_info_reports_install_status_ready(tmp_path: Path) -> None:
    """SE.8 — /ui/api/server-info carries status='ready' when venv live."""
    from test_ui_api import _make_ui_app  # test harness

    scanner._esphome_ready.set()
    scanner._server_esphome_bin = "/fake/bin/esphome"
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/server-info")
        assert resp.status == 200
        data = await resp.json()
        assert data["esphome_install_status"] == "ready"
        assert "esphome_server_version" in data
    finally:
        await ta.close()


async def test_server_info_reports_install_status_installing(tmp_path: Path) -> None:
    """SE.8 — status='installing' when ready event is clear and no failure."""
    from test_ui_api import _make_ui_app

    # autouse fixture already cleared _esphome_ready.
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/server-info")
        data = await resp.json()
        assert data["esphome_install_status"] == "installing"
    finally:
        await ta.close()


async def test_server_info_reports_install_status_failed(tmp_path: Path) -> None:
    """SE.8 — status='failed' when the install task flagged a failure."""
    from test_ui_api import _make_ui_app

    scanner._esphome_install_failed = True
    ta = await _make_ui_app(tmp_path)
    try:
        resp = await ta.get("/ui/api/server-info")
        data = await resp.json()
        assert data["esphome_install_status"] == "failed"
    finally:
        await ta.close()


async def test_reinstall_endpoint_clears_failure_and_schedules_install(tmp_path: Path) -> None:
    """SE.8 — POST /ui/api/esphome/reinstall clears failure flag + returns ok."""
    from test_ui_api import _make_ui_app
    from unittest.mock import AsyncMock

    scanner._esphome_install_failed = True
    scanner._selected_esphome_version = "2026.4.0"
    ta = await _make_ui_app(tmp_path)
    try:
        # Patch ensure_esphome_installed so the background task doesn't
        # actually try to hit the network.
        with patch.object(scanner, "ensure_esphome_installed", AsyncMock()):
            resp = await ta.post("/ui/api/esphome/reinstall", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["version"] == "2026.4.0"
        assert scanner._esphome_install_failed is False
        assert not scanner._esphome_ready.is_set()
    finally:
        await ta.close()
