"""Unit tests for client version management and timeout behavior."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from version_manager import VersionManager


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vm(tmp_path):
    """VersionManager with tmp_path as base and max 3 versions."""
    return VersionManager(versions_base=tmp_path, max_versions=3)


def _add_fake_version(tmp_path: Path, version: str) -> None:
    """Create a fake installed version directory with a stub esphome binary."""
    venv = tmp_path / version / "bin"
    venv.mkdir(parents=True, exist_ok=True)
    esphome = venv / "esphome"
    esphome.write_text("#!/bin/sh\necho fake esphome\n")
    esphome.chmod(0o755)


# ---------------------------------------------------------------------------
# VersionManager: basic operations
# ---------------------------------------------------------------------------

def test_installed_versions_empty(vm):
    assert vm.installed_versions() == []


def test_get_esphome_path_raises_if_not_installed(vm):
    with pytest.raises(FileNotFoundError):
        vm.get_esphome_path("9.9.9")


def test_get_esphome_path_returns_path_when_installed(tmp_path):
    _add_fake_version(tmp_path, "2024.3.1")
    vm = VersionManager(versions_base=tmp_path, max_versions=3)
    path = vm.get_esphome_path("2024.3.1")
    assert path.endswith("esphome")
    assert Path(path).exists()


def test_installed_versions_loaded_from_disk(tmp_path):
    _add_fake_version(tmp_path, "2024.1.0")
    _add_fake_version(tmp_path, "2024.2.0")
    vm = VersionManager(versions_base=tmp_path, max_versions=3)
    versions = vm.installed_versions()
    assert "2024.1.0" in versions
    assert "2024.2.0" in versions


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------

def test_eviction_at_limit_plus_one(tmp_path):
    """When 3 versions are installed and a 4th is requested, LRU is evicted."""
    _add_fake_version(tmp_path, "2024.1.0")
    _add_fake_version(tmp_path, "2024.2.0")
    _add_fake_version(tmp_path, "2024.3.0")
    vm = VersionManager(versions_base=tmp_path, max_versions=3)

    # Access order: 2024.1.0, 2024.2.0, 2024.3.0 (oldest = 2024.1.0)
    # Request a 4th version — pip install will be called
    with patch.object(vm, "_install") as mock_install:
        def fake_install(version):
            _add_fake_version(tmp_path, version)

        mock_install.side_effect = fake_install
        vm.ensure_version("2024.4.0")

    # 2024.1.0 (LRU) should have been evicted
    remaining = vm.installed_versions()
    assert "2024.1.0" not in remaining
    assert "2024.4.0" in remaining
    assert len(remaining) <= 3


def test_eviction_respects_lru_order(tmp_path):
    """After accessing version 1, version 2 becomes the LRU."""
    _add_fake_version(tmp_path, "v1")
    _add_fake_version(tmp_path, "v2")
    _add_fake_version(tmp_path, "v3")
    vm = VersionManager(versions_base=tmp_path, max_versions=3)

    # Access v1 to make it MRU
    vm.get_esphome_path("v1")  # now LRU = v2

    evicted = []

    def fake_evict(keep_version=None):
        for version in vm._lru:
            if version == keep_version:
                continue
            evicted.append(version)
            del vm._lru[version]
            return True
        return False

    with patch.object(vm, "_evict_lru", side_effect=fake_evict):
        with patch.object(vm, "_install", side_effect=lambda v: _add_fake_version(tmp_path, v)):
            vm.ensure_version("v4")

    assert evicted[0] == "v2", f"Expected v2 to be evicted, got {evicted}"


def test_eviction_preserves_server_active_version(tmp_path):
    """#119 (round 2): the shared-dir local worker must never evict the
    venv the server published as its active bundling venv.

    Regression: without the sentinel pin, installing a new version with
    MAX_ESPHOME_VERSIONS=1 evicted the server's selected venv, leaving
    scanner._server_esphome_bin dangling and every bundle failing with
    FileNotFoundError until the add-on restarted.
    """
    import os

    from version_manager import write_server_active_version

    # Server-active venv (oldest mtime → first eviction candidate).
    _add_fake_version(tmp_path, "2025.11.0")
    _add_fake_version(tmp_path, "2026.1.0")
    # Force a deterministic LRU order: server-active is least-recent.
    os.utime(tmp_path / "2025.11.0", (1.0, 1.0))
    os.utime(tmp_path / "2026.1.0", (2.0, 2.0))

    write_server_active_version(tmp_path, "2025.11.0")

    vm = VersionManager(versions_base=tmp_path, max_versions=1)
    with patch.object(vm, "_install", side_effect=lambda v: _add_fake_version(tmp_path, v)):
        vm.ensure_version("2026.5.1")

    remaining = vm.installed_versions()
    assert "2025.11.0" in remaining, "server-active venv must survive eviction"
    assert "2026.5.1" in remaining, "newly-installed version present"
    assert "2026.1.0" not in remaining, "unprotected old venv evicted"


def test_eviction_no_infinite_loop_when_only_protected_remain(tmp_path):
    """When every evictable venv is server-pinned, the install eviction
    loop must terminate (and exceed max_versions) rather than spin."""
    from version_manager import write_server_active_version

    _add_fake_version(tmp_path, "2025.11.0")
    write_server_active_version(tmp_path, "2025.11.0")

    vm = VersionManager(versions_base=tmp_path, max_versions=1)
    with patch.object(vm, "_install", side_effect=lambda v: _add_fake_version(tmp_path, v)):
        # Must return (no hang) even though the only existing venv is pinned
        # and we're already at max_versions.
        vm.ensure_version("2026.5.1")

    remaining = vm.installed_versions()
    assert "2025.11.0" in remaining
    assert "2026.5.1" in remaining


def test_read_server_active_versions_absent_is_empty(tmp_path):
    """Remote workers (own dir, no sentinel) see no pins."""
    from version_manager import read_server_active_versions

    assert read_server_active_versions(tmp_path) == set()


def test_no_eviction_under_limit(tmp_path):
    """Installing a version when under the limit should not evict anything."""
    _add_fake_version(tmp_path, "v1")
    _add_fake_version(tmp_path, "v2")
    vm = VersionManager(versions_base=tmp_path, max_versions=3)

    with patch.object(vm, "_evict_lru") as mock_evict:
        with patch.object(vm, "_install", side_effect=lambda v: _add_fake_version(tmp_path, v)):
            vm.ensure_version("v3")

    mock_evict.assert_not_called()


def test_already_installed_no_reinstall(tmp_path):
    """ensure_version on an already-installed version must not call _install."""
    _add_fake_version(tmp_path, "2024.3.1")
    vm = VersionManager(versions_base=tmp_path, max_versions=3)

    with patch.object(vm, "_install") as mock_install:
        path = vm.ensure_version("2024.3.1")

    mock_install.assert_not_called()
    assert "esphome" in path


def test_ensure_version_updates_lru(tmp_path):
    """Accessing a version should move it to the end (MRU) in the LRU dict."""
    _add_fake_version(tmp_path, "v1")
    _add_fake_version(tmp_path, "v2")
    vm = VersionManager(versions_base=tmp_path, max_versions=3)

    # v1 and v2 loaded; v1 first (LRU)
    vm.ensure_version("v1")  # access v1 -> move to MRU

    lru_keys = list(vm._lru.keys())
    assert lru_keys[-1] == "v1", f"v1 should be MRU; got {lru_keys}"


# ---------------------------------------------------------------------------
# Subprocess timeout simulation (tests _run_subprocess indirectly)
# ---------------------------------------------------------------------------

def test_run_subprocess_success(tmp_path):
    """Import and test _run_subprocess directly."""
    import client as client_module  # noqa: PLC0415

    log, ok = client_module._run_subprocess(
        [sys.executable, "-c", "print('hello')"],
        cwd=str(tmp_path),
        timeout=10,
        label="test",
    )
    assert ok
    assert "hello" in log


def test_run_subprocess_failure(tmp_path):
    import client as client_module  # noqa: PLC0415

    log, ok = client_module._run_subprocess(
        [sys.executable, "-c", "import sys; sys.exit(1)"],
        cwd=str(tmp_path),
        timeout=10,
        label="test-fail",
    )
    assert not ok


def test_run_subprocess_timeout(tmp_path):
    """A process that sleeps longer than timeout should be killed and return TIMED OUT."""
    import client as client_module  # noqa: PLC0415

    log, ok = client_module._run_subprocess(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
        timeout=1,
        label="test-timeout",
    )
    assert not ok
    assert "TIMED OUT" in log


def test_run_subprocess_captures_output(tmp_path):
    import client as client_module  # noqa: PLC0415

    log, ok = client_module._run_subprocess(
        [sys.executable, "-c", "print('stdout line'); import sys; print('stderr line', file=sys.stderr)"],
        cwd=str(tmp_path),
        timeout=10,
        label="test-output",
    )
    assert ok
    assert "stdout line" in log


# ---------------------------------------------------------------------------
# client.py: ensure SERVER_URL and SERVER_TOKEN env vars are set for import
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_required_env(monkeypatch):
    monkeypatch.setenv("SERVER_URL", "http://localhost:8765")
    monkeypatch.setenv("SERVER_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# B.1 — VersionManager concurrency stress tests
# ---------------------------------------------------------------------------

def test_ensure_version_concurrent_same_version_installs_once(tmp_path):
    """10 threads requesting the same version must trigger exactly one install.

    The other 9 threads must block on the shared ``_installing`` event and
    return the same esphome binary path once the installer finishes. Regression
    for the race where two threads both decide to install before either sets
    ``_installing[version]``.
    """
    vm = VersionManager(versions_base=tmp_path, max_versions=3)

    install_calls: list[str] = []
    install_call_lock = threading.Lock()
    install_started = threading.Event()
    release_install = threading.Event()

    def slow_install(version: str) -> None:
        """Simulate a slow pip install — hold the version lock so all other
        threads have time to arrive at the waiter branch."""
        with install_call_lock:
            install_calls.append(version)
        install_started.set()
        # Block until the test tells us to finish, so we can verify the other
        # 9 threads are all blocked on the shared install event.
        release_install.wait(timeout=5)
        _add_fake_version(tmp_path, version)

    results: list[str] = []
    errors: list[Exception] = []
    results_lock = threading.Lock()

    def worker() -> None:
        try:
            path = vm.ensure_version("2026.3.3")
            with results_lock:
                results.append(path)
        except Exception as exc:  # pragma: no cover — fail the test if hit
            errors.append(exc)

    with patch.object(vm, "_install", side_effect=slow_install):
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()

        # Wait for the installer thread to actually start, then give the
        # other 9 threads a beat to arrive at the wait branch.
        assert install_started.wait(timeout=2), "installer did not start"
        time.sleep(0.2)

        # Release the installer so every thread can proceed.
        release_install.set()

        for t in threads:
            t.join(timeout=10)

    assert not errors, f"worker threads raised: {errors}"
    assert len(results) == 10
    # Exactly one install call despite 10 concurrent requests.
    assert install_calls == ["2026.3.3"], (
        f"expected single install, got {install_calls}"
    )
    # All threads returned the same binary path.
    assert len(set(results)) == 1


def test_ensure_version_concurrent_distinct_versions_all_install(tmp_path):
    """Multiple threads requesting different versions each trigger their own
    install in parallel. No deadlock, no cross-contamination."""
    vm = VersionManager(versions_base=tmp_path, max_versions=10)

    install_calls: list[str] = []
    install_call_lock = threading.Lock()

    def fake_install(version: str) -> None:
        with install_call_lock:
            install_calls.append(version)
        # A brief sleep so the installs overlap in time.
        time.sleep(0.05)
        _add_fake_version(tmp_path, version)

    errors: list[Exception] = []

    def worker(version: str) -> None:
        try:
            vm.ensure_version(version)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    with patch.object(vm, "_install", side_effect=fake_install):
        versions = [f"2026.{i}.0" for i in range(5)]
        threads = [threading.Thread(target=worker, args=(v,)) for v in versions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    assert not errors
    assert sorted(install_calls) == sorted(versions)
    # LRU contains exactly the 5 installed versions (max was 10, no eviction).
    assert set(vm.installed_versions()) == set(versions)


def test_ensure_version_lru_full_preserves_keep_version_under_contention(tmp_path):
    """When the LRU is full and a new version is being installed, the
    installing version must never be evicted even while another thread is
    actively asking for it (``keep_version`` contract)."""
    # Pre-seed max_versions distinct installs.
    _add_fake_version(tmp_path, "v1")
    _add_fake_version(tmp_path, "v2")
    _add_fake_version(tmp_path, "v3")
    vm = VersionManager(versions_base=tmp_path, max_versions=3)

    # Touch v2 and v3 so v1 is the LRU.
    vm.get_esphome_path("v2")
    vm.get_esphome_path("v3")

    install_started = threading.Event()
    release_install = threading.Event()

    def slow_install(version: str) -> None:
        install_started.set()
        release_install.wait(timeout=5)
        _add_fake_version(tmp_path, version)

    errors: list[Exception] = []

    def installer() -> None:
        try:
            vm.ensure_version("v4")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    with patch.object(vm, "_install", side_effect=slow_install):
        t_install = threading.Thread(target=installer)
        t_install.start()

        # Wait for the installer to start. At this point v1 has been evicted
        # (to make room for v4) and v4 is in _installing.
        assert install_started.wait(timeout=2)

        # Spawn another thread that asks for v4 — it should block on the
        # install event and never cause v4 itself to be evicted.
        waiter_done = threading.Event()

        def waiter() -> None:
            try:
                vm.ensure_version("v4")
            finally:
                waiter_done.set()

        t_wait = threading.Thread(target=waiter)
        t_wait.start()

        # Release the installer.
        release_install.set()

        t_install.join(timeout=10)
        t_wait.join(timeout=10)
        assert waiter_done.is_set()

    assert not errors
    # v1 was the LRU, so it should have been evicted to make room for v4.
    # v4 must still be present — neither thread should have evicted the version
    # currently being installed.
    remaining = set(vm.installed_versions())
    assert "v4" in remaining
    assert "v1" not in remaining
    assert len(remaining) <= 3


# ---------------------------------------------------------------------------
# B.4 — OTA retry regression test (bug #177)
#
# The retry path after a successful compile + failed OTA must use
# ``esphome upload``, NOT ``esphome run``, and must NOT pass ``--no-logs``
# (which ``esphome upload`` rejects with "unrecognized arguments").
# ---------------------------------------------------------------------------

def test_run_job_ota_retry_uses_upload_without_no_logs(tmp_path, monkeypatch):
    import client as client_module

    # The run_job function touches a lot — install, extract, subprocess,
    # result submission. We stub every collaborator so only the command-
    # construction logic runs.
    _add_fake_version(tmp_path, "2024.3.1")

    # #13: run_job now uses a stable per-target build dir under
    # _ESPHOME_VERSIONS_DIR. Point it at tmp_path so the test doesn't
    # try to write to /esphome-versions/.
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    commands: list[list[str]] = []

    def fake_run_subprocess(cmd, cwd, timeout, label, env=None, job_id=None):
        commands.append(list(cmd))
        # First call = "compile+OTA" via `esphome run`: simulate a compile
        # success + OTA failure. Subsequent call = retry via `esphome upload`:
        # simulate a fresh (uninteresting) success.
        if label == "compile+OTA":
            return (
                "INFO Successfully compiled program.\nERROR Error resolving OTA target: Connect failed\n",
                False,
            )
        return ("upload ok", True)

    submitted: list[tuple[str, str, object]] = []

    def fake_submit(job_id, status, log=None, ota_result=None):
        submitted.append((status, ota_result, log))

    monkeypatch.setattr(client_module, "_run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(client_module, "_submit_result", fake_submit)
    monkeypatch.setattr(client_module, "_flush_log_text", lambda *a, **k: None)
    monkeypatch.setattr(client_module, "_log_invocation", lambda *a, **k: None)
    monkeypatch.setattr(client_module, "_report_status", lambda *a, **k: None)
    # Skip the OTA diagnostics network calls on failure path.
    monkeypatch.setattr(client_module, "_ota_network_diagnostics", lambda *a, **k: "")
    # Skip the 5-second sleep between compile and retry.
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)

    # Minimal bundle: a tar.gz containing a single empty target YAML.
    import base64
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"esphome:\n  name: dev\n"
        info = tarfile.TarInfo(name="dev.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    bundle_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    vm = VersionManager(versions_base=tmp_path, max_versions=3)
    job = {
        "job_id": "j1",
        "target": "dev.yaml",
        "esphome_version": "2024.3.1",
        "bundle_b64": bundle_b64,
        "timeout_seconds": 60,
        "ota_only": False,
        "validate_only": False,
        "ota_address": "10.0.0.5",
    }

    client_module.run_job("client-1", job, vm, worker_id=1)

    # Exactly two subprocess invocations: compile+OTA run, then upload retry.
    assert len(commands) == 2, f"expected 2 subprocess calls, got {len(commands)}: {commands}"

    first, second = commands
    # First call: `esphome run ... --no-logs ... --device 10.0.0.5`
    assert "run" in first and "--no-logs" in first and "--device" in first
    assert "10.0.0.5" in first

    # Second call: `esphome upload` — MUST NOT contain --no-logs or `run`.
    assert "upload" in second, f"retry must use 'upload' verb: {second}"
    assert "run" not in second, f"retry must NOT use 'run' verb: {second}"
    assert "--no-logs" not in second, (
        f"retry command must NOT pass --no-logs (esphome upload rejects it): {second}"
    )
    assert "--device" in second and "10.0.0.5" in second

    # The result submission records the OTA retry succeeded.
    assert submitted[-1][0] == "success"
    assert submitted[-1][1] == "success"


# ---------------------------------------------------------------------------
# #45 — Per-slot working dirs + shared per-target cache
# ---------------------------------------------------------------------------


def test_slot_and_cache_dir_helpers(tmp_path, monkeypatch):
    """_slot_dir and _cache_dir compose the expected paths under the base."""
    import client as client_module  # noqa: PLC0415
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))
    assert client_module._slot_dir(2, "kitchen") == str(tmp_path / "slots" / "2" / "kitchen")
    assert client_module._cache_dir("kitchen") == str(tmp_path / "cache" / "kitchen")


def test_copytree_replace_overwrites_existing(tmp_path):
    """_copytree_replace wipes the destination tree before copying."""
    import client as client_module  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "new.txt").write_text("new")

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "stale.txt").write_text("stale")

    client_module._copytree_replace(str(src), str(dst))

    # Old file is gone, new file is present
    assert not (dst / "stale.txt").exists()
    assert (dst / "new.txt").read_text() == "new"


def test_copytree_replace_noop_when_src_missing(tmp_path):
    """Missing source is a silent no-op (dst left intact)."""
    import client as client_module  # noqa: PLC0415
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "keep.txt").write_text("keep")
    client_module._copytree_replace(str(tmp_path / "missing"), str(dst))
    assert (dst / "keep.txt").read_text() == "keep"


def test_sync_cache_into_slot_seeds_pio_on_first_compile(tmp_path, monkeypatch):
    """Slot with no .pio/ pulls from the shared cache on sync-in."""
    import client as client_module  # noqa: PLC0415
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    # Seed cache
    cache_pio = tmp_path / "cache" / "dev" / ".pio" / "build"
    cache_pio.mkdir(parents=True)
    (cache_pio / "firmware.o").write_text("obj")

    # Empty slot dir
    slot_dir = tmp_path / "slots" / "1" / "dev"
    slot_dir.mkdir(parents=True)

    client_module._sync_cache_into_slot("dev", str(slot_dir))

    # .pio/ is now populated in the slot dir
    assert (slot_dir / ".pio" / "build" / "firmware.o").read_text() == "obj"


def test_sync_cache_into_slot_skips_when_slot_already_has_pio(tmp_path, monkeypatch):
    """If the slot already has its own .pio/, the sync-in is a no-op — the
    slot's local cache is more relevant than the shared one."""
    import client as client_module  # noqa: PLC0415
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    # Cache has one version
    cache_pio = tmp_path / "cache" / "dev" / ".pio"
    cache_pio.mkdir(parents=True)
    (cache_pio / "shared.txt").write_text("cache")

    # Slot already has its own version
    slot_pio = tmp_path / "slots" / "1" / "dev" / ".pio"
    slot_pio.mkdir(parents=True)
    (slot_pio / "local.txt").write_text("slot")

    client_module._sync_cache_into_slot("dev", str(slot_pio.parent))

    # Slot kept its own state, didn't adopt shared cache
    assert (slot_pio / "local.txt").exists()
    assert not (slot_pio / "shared.txt").exists()


def test_sync_slot_into_cache_promotes_to_shared(tmp_path, monkeypatch):
    """After a successful compile, slot .pio/ is promoted to the shared cache."""
    import client as client_module  # noqa: PLC0415
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    slot_pio = tmp_path / "slots" / "2" / "dev" / ".pio" / "build"
    slot_pio.mkdir(parents=True)
    (slot_pio / "firmware.bin").write_text("binary")

    client_module._sync_slot_into_cache("dev", str(slot_pio.parent.parent))

    cache_firmware = tmp_path / "cache" / "dev" / ".pio" / "build" / "firmware.bin"
    assert cache_firmware.read_text() == "binary"


def test_sync_slot_into_cache_replaces_old_cache(tmp_path, monkeypatch):
    """Sync-out replaces the entire cache tree (stale files in cache removed)."""
    import client as client_module  # noqa: PLC0415
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    # Old cache has a stale artifact
    cache_build = tmp_path / "cache" / "dev" / ".pio" / "build"
    cache_build.mkdir(parents=True)
    (cache_build / "stale.o").write_text("stale")

    # Slot has a fresh compile result
    slot_build = tmp_path / "slots" / "1" / "dev" / ".pio" / "build"
    slot_build.mkdir(parents=True)
    (slot_build / "fresh.o").write_text("fresh")

    client_module._sync_slot_into_cache("dev", str(slot_build.parent.parent))

    # Stale gone, fresh present
    assert not (cache_build / "stale.o").exists()
    assert (cache_build / "fresh.o").read_text() == "fresh"


def test_target_cache_lock_is_exclusive(tmp_path, monkeypatch):
    """Two threads trying to acquire the same per-target lock are serialized."""
    import client as client_module  # noqa: PLC0415
    import threading as _threading
    import time as _time
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    events: list[tuple[str, float]] = []
    start = _threading.Event()

    def worker(name: str, hold_for: float) -> None:
        start.wait()
        with client_module._target_cache_lock("dev"):
            events.append((f"{name}-acquired", _time.monotonic()))
            _time.sleep(hold_for)
            events.append((f"{name}-released", _time.monotonic()))

    t1 = _threading.Thread(target=worker, args=("a", 0.15))
    t2 = _threading.Thread(target=worker, args=("b", 0.05))
    t1.start()
    t2.start()
    start.set()
    t1.join()
    t2.join()

    # Exactly 4 events, first-acquired then first-released then second-acquired then second-released
    assert len(events) == 4
    seq = [e[0].split("-")[1] for e in events]
    assert seq == ["acquired", "released", "acquired", "released"], (
        f"lock not exclusive, events: {events}"
    )


def test_clean_build_cache_preserves_esphome_venvs_and_pio_slots(tmp_path, monkeypatch):
    """Regression for #119 + #214: ``Clean Cache`` must preserve ESPHome
    venvs (anything with ``bin/esphome``) AND PlatformIO core dirs
    (``pio-slot-*``) so the toolchain isn't re-downloaded.

    #119: pre-fix wiped venvs and stranded the server's bundle subprocess
    on a deleted ``bin/python``.

    #214: pre-fix wiped ``pio-slot-N`` (PlatformIO's toolchain home),
    forcing every subsequent compile through a 5–10 min toolchain
    re-download and surfacing the partial-extract case as ``cc1:
    posix_spawnp: No such file or directory``.

    Anything else (slots, cache, platformio, builds) is build-artifact
    state — that's what the user is asking us to clean.
    """
    import client as client_module  # noqa: PLC0415
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    # Two ESPHome venvs (have bin/esphome) — must survive the clean.
    for ver in ("2026.4.2", "2026.4.3"):
        (tmp_path / ver / "bin").mkdir(parents=True)
        (tmp_path / ver / "bin" / "esphome").write_text("#!/bin/sh\n")

    # Two PlatformIO core dirs — must also survive (toolchain home).
    (tmp_path / "pio-slot-1" / "packages" / "toolchain-xtensa-esp-elf").mkdir(parents=True)
    (tmp_path / "pio-slot-2" / "packages" / "toolchain-xtensa-esp-elf").mkdir(parents=True)

    # Build cache directories — these are what Clean Cache should wipe.
    (tmp_path / "cache" / "dev").mkdir(parents=True)
    (tmp_path / "slots" / "1" / "dev").mkdir(parents=True)
    (tmp_path / "platformio").mkdir()

    client_module._clean_build_cache()

    # Venvs preserved
    assert (tmp_path / "2026.4.2" / "bin" / "esphome").exists()
    assert (tmp_path / "2026.4.3" / "bin" / "esphome").exists()
    # PlatformIO core dirs preserved (#214)
    assert (tmp_path / "pio-slot-1" / "packages" / "toolchain-xtensa-esp-elf").exists()
    assert (tmp_path / "pio-slot-2" / "packages" / "toolchain-xtensa-esp-elf").exists()

    # Build caches gone
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "slots").exists()
    assert not (tmp_path / "platformio").exists()


def test_clean_build_cache_removes_unprotected_dirs(tmp_path, monkeypatch):
    """Directories that aren't venvs or pio-slot-* are removed."""
    import client as client_module  # noqa: PLC0415
    monkeypatch.setattr(client_module, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    (tmp_path / "cache" / "dev").mkdir(parents=True)
    (tmp_path / "slots").mkdir()
    (tmp_path / "platformio").mkdir()

    client_module._clean_build_cache()

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# #214 — _wipe_broken_toolchain — when a compile fails with cc1 ENOENT,
# the worker auto-heals by removing the broken PlatformIO toolchain so
# the next compile re-extracts. Live repro on macdaddy.localdomain in
# the home-lab fleet: 3-of-65 jobs in a "Compile All Online" run failed
# with `xtensa-esp-elf-gcc: fatal error: cannot execute 'cc1':
# posix_spawnp: No such file or directory`, all on the same worker's
# pio-slot-1.
# ---------------------------------------------------------------------------

def test_wipe_broken_toolchain_removes_packages_and_penv(tmp_path):
    import client as client_module  # noqa: PLC0415

    pio_dir = tmp_path / "pio-slot-1"
    packages = pio_dir / "packages" / "toolchain-xtensa-esp-elf" / "bin"
    packages.mkdir(parents=True)
    (packages / "xtensa-esp-elf-gcc").write_text("broken-binary")
    # #220: penv/ is also wiped — covers the `penv/bin/esptool: not found`
    # / `Missing framework-…-libs package` failure modes that the cc1-only
    # wipe missed.
    penv_bin = pio_dir / "penv" / "bin"
    penv_bin.mkdir(parents=True)
    (penv_bin / "esptool").write_text("broken-script")
    # PIO core dir's own siblings should be untouched.
    (pio_dir / "dist").mkdir()
    (pio_dir / "dist" / "downloaded.tar.gz").write_text("keep — saves a re-download")
    (pio_dir / "appstate.json").write_text("keep")

    assert client_module._wipe_broken_toolchain(str(pio_dir)) is True
    assert not (pio_dir / "packages").exists()
    assert not (pio_dir / "penv").exists()
    # Siblings untouched.
    assert (pio_dir / "dist" / "downloaded.tar.gz").read_text() == "keep — saves a re-download"
    assert (pio_dir / "appstate.json").read_text() == "keep"


def test_wipe_broken_toolchain_returns_true_when_only_one_subtree_present(tmp_path):
    """Either packages/ or penv/ alone is enough to count as a wipe."""
    import client as client_module  # noqa: PLC0415

    pio_dir = tmp_path / "pio-slot-1"
    (pio_dir / "penv" / "bin").mkdir(parents=True)
    # No packages/ — the heal still does what it can.

    assert client_module._wipe_broken_toolchain(str(pio_dir)) is True
    assert not (pio_dir / "penv").exists()


def test_wipe_broken_toolchain_noop_when_nothing_to_wipe(tmp_path):
    """Tolerate the case where there's neither packages/ nor penv/."""
    import client as client_module  # noqa: PLC0415

    pio_dir = tmp_path / "pio-slot-1"
    pio_dir.mkdir()

    assert client_module._wipe_broken_toolchain(str(pio_dir)) is False


def test_wipe_broken_toolchain_swallows_rmtree_failure(tmp_path, monkeypatch):
    """A read-only filesystem mid-rmtree must not propagate — the worker
    still needs to submit ``failed`` and move on."""
    import client as client_module  # noqa: PLC0415

    pio_dir = tmp_path / "pio-slot-1"
    (pio_dir / "packages").mkdir(parents=True)
    (pio_dir / "penv").mkdir(parents=True)

    def fake_rmtree(path, ignore_errors=False):
        raise PermissionError("read-only fs")

    monkeypatch.setattr(client_module.shutil, "rmtree", fake_rmtree)
    assert client_module._wipe_broken_toolchain(str(pio_dir)) is False


def test_wipe_broken_toolchain_recovers_from_strict_rmtree_enoent_mid_walk(tmp_path, monkeypatch):
    """Live repro on AI-MacBook-Pro 2026-05-02: strict ``shutil.rmtree``
    raised ``FileNotFoundError: 'CMakeLists.txt'`` mid-walk on
    ``packages/`` (concurrent mutator / already-removed inode), the
    single-pass wipe abandoned the rest of the subtree, and the four
    follow-up screek-2a-N compiles cascaded on the half-rotted state.
    After the strict pass raises a benign ENOENT, the wipe must do a
    lenient ``ignore_errors=True`` sweep so the next compile gets a
    clean slate.
    """
    import client as client_module  # noqa: PLC0415

    pio_dir = tmp_path / "pio-slot-1"
    # Real on-disk state — the strict pass will be intercepted but the
    # lenient sweep needs an actual tree to clean.
    (pio_dir / "packages" / "tool-esptoolpy").mkdir(parents=True)
    (pio_dir / "packages" / "tool-esptoolpy" / "leftover").write_text("partially-extracted")
    (pio_dir / "penv" / "bin").mkdir(parents=True)
    (pio_dir / "penv" / "bin" / "esptool").write_text("broken-script")

    real_rmtree = client_module.shutil.rmtree
    strict_seen = {"count": 0}

    def flaky_rmtree(path, ignore_errors=False):
        if not ignore_errors:
            strict_seen["count"] += 1
            # Mirror the live error string — relative inner filename.
            raise FileNotFoundError(2, "No such file or directory", "CMakeLists.txt")
        return real_rmtree(path, ignore_errors=True)

    monkeypatch.setattr(client_module.shutil, "rmtree", flaky_rmtree)

    assert client_module._wipe_broken_toolchain(str(pio_dir)) is True
    # Both subtrees were ultimately swept by the lenient retry, even
    # though the strict pass raised on each.
    assert not (pio_dir / "packages").exists()
    assert not (pio_dir / "penv").exists()
    assert strict_seen["count"] == 2  # one strict attempt per subtree


# ---------------------------------------------------------------------------
# #220 — _is_broken_pio_state — every distinct corruption symptom we've
# seen in the home lab must trigger the self-heal path. New patterns get
# added to ``_BROKEN_PIO_SIGNATURES`` and a fixture line goes here.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "log_excerpt",
    [
        # #214 original — toolchain-xtensa-esp-elf missing cc1.
        "xtensa-esp-elf-gcc: fatal error: cannot execute 'cc1': posix_spawnp: No such file or directory",
        # #220 — framework-libs partially extracted.
        "Error: Missing framework-arduinoespressif32-libs package",
        # #220 — tool-* package missing pyproject.toml/setup.py.
        "error: /esphome-versions/pio-slot-1/packages/tool-esptoolpy does not appear to be a Python project, "
        "as neither `pyproject.toml` nor `setup.py` are present in the directory",
        # #220 — esptool half-installed in penv/.espidf-*/.
        "ModuleNotFoundError: No module named 'esptool.__init__'; 'esptool' is not a package",
        # #220 — penv binary missing → Error 127.
        "sh: 1: /esphome-versions/pio-slot-2/penv/bin/esptool: not found\n"
        "*** [.pioenvs/bbq-gas-valve/bootloader.bin] Error 127",
        # #220 — generic Error 127 in firmware build.
        "*** [.pioenvs/screek-1u-1/firmware.bin] Error 127",
    ],
)
def test_is_broken_pio_state_matches_real_lab_failures(log_excerpt):
    import client as client_module  # noqa: PLC0415

    assert client_module._is_broken_pio_state(log_excerpt) is True


@pytest.mark.parametrize(
    "log_excerpt",
    [
        # Clean compile log — should not trigger.
        "Compiling .pioenvs/foo/firmware.bin\nLinking .pioenvs/foo/firmware.elf\n",
        # User config error — different bug class, must NOT self-heal.
        "ERROR Error: Unable to find component 'invalid_platform'",
        # OTA failure — different bug class, must NOT self-heal.
        "ERROR Error resolving IP address of 192.168.1.99: [Errno 8] nodename nor servname provided",
        # Compile error in user code — different bug class.
        "main.cpp:42:1: error: 'foo' was not declared in this scope",
        # Other Error 127 (not in .pioenvs/) — keep narrow.
        "make: *** [unrelated/target] Error 127",
    ],
)
def test_is_broken_pio_state_ignores_non_corruption_failures(log_excerpt):
    import client as client_module  # noqa: PLC0415

    assert client_module._is_broken_pio_state(log_excerpt) is False


# ---------------------------------------------------------------------------
# DQ.8 — version manager defaults to 1 venv; legacy MAX_ESPHOME_VERSIONS warns
# ---------------------------------------------------------------------------


def test_version_manager_module_default_is_1():
    """DQ.8: the module-level constant is 1 — disk_quota engine bounds the rest."""
    import version_manager as vm_mod  # noqa: PLC0415

    assert vm_mod.MAX_ESPHOME_VERSIONS == 1


# ---------------------------------------------------------------------------
# DQ.9 — disk-quota wiring in client.py
# ---------------------------------------------------------------------------


def test_parse_disk_quota_gb_env_accepts_integer():
    import client as client_mod  # noqa: PLC0415
    assert client_mod._parse_disk_quota_gb_env("5") == 5 * 1024 ** 3


def test_parse_disk_quota_gb_env_rejects_non_integer():
    import client as client_mod  # noqa: PLC0415
    assert client_mod._parse_disk_quota_gb_env("5.5") is None
    assert client_mod._parse_disk_quota_gb_env("abc") is None


def test_parse_disk_quota_gb_env_rejects_below_one():
    import client as client_mod  # noqa: PLC0415
    assert client_mod._parse_disk_quota_gb_env("0") is None
    assert client_mod._parse_disk_quota_gb_env("-1") is None


def test_parse_disk_quota_gb_env_treats_blank_as_none():
    import client as client_mod  # noqa: PLC0415
    assert client_mod._parse_disk_quota_gb_env(None) is None
    assert client_mod._parse_disk_quota_gb_env("") is None
    assert client_mod._parse_disk_quota_gb_env("   ") is None


def test_disk_quota_cell_round_trip():
    import client as client_mod  # noqa: PLC0415
    prev = client_mod._get_current_disk_quota_bytes()
    try:
        client_mod._set_current_disk_quota_bytes(7 * 1024 ** 3)
        assert client_mod._get_current_disk_quota_bytes() == 7 * 1024 ** 3
        client_mod._set_current_disk_quota_bytes(None)
        assert client_mod._get_current_disk_quota_bytes() is None
    finally:
        client_mod._set_current_disk_quota_bytes(prev)


def test_run_disk_quota_sweep_no_op_when_quota_unset(tmp_path, monkeypatch):
    """DQ.9: pre-first-heartbeat the cell is None; sweep skips silently."""
    import client as client_mod  # noqa: PLC0415

    monkeypatch.setattr(client_mod, "_ESPHOME_VERSIONS_DIR", str(tmp_path))
    prev = client_mod._get_current_disk_quota_bytes()
    try:
        client_mod._set_current_disk_quota_bytes(None)
        # Should not raise even though disk_quota engine has nothing to do.
        client_mod._run_disk_quota_sweep(label="test")
        assert client_mod._get_last_eviction_freed_bytes() == 0
    finally:
        client_mod._set_current_disk_quota_bytes(prev)


def test_run_disk_quota_sweep_evicts_when_over_quota(tmp_path, monkeypatch):
    """DQ.9: when the cell is set and tree is over budget, sweep evicts."""
    import client as client_mod  # noqa: PLC0415

    # Make a tiny synthetic tree: one venv + one cache target.
    (tmp_path / "2026.4.3" / "bin").mkdir(parents=True)
    (tmp_path / "2026.4.3" / "bin" / "esphome").write_bytes(b"")
    (tmp_path / "2026.4.3" / "lib").mkdir()
    (tmp_path / "2026.4.3" / "lib" / "padding").write_bytes(b"\x00" * 100)

    cache = tmp_path / "cache" / "device-a"
    cache.mkdir(parents=True)
    (cache / "blob").write_bytes(b"\x00" * 5000)

    monkeypatch.setattr(client_mod, "_ESPHOME_VERSIONS_DIR", str(tmp_path))
    prev = client_mod._get_current_disk_quota_bytes()
    try:
        # Quota tight enough that the cache must go (only the venv fits).
        client_mod._set_current_disk_quota_bytes(500)
        client_mod._run_disk_quota_sweep(label="test")
        assert not (tmp_path / "cache" / "device-a").exists()
        assert client_mod._get_last_eviction_freed_bytes() > 0
    finally:
        client_mod._set_current_disk_quota_bytes(prev)


def test_active_job_set_pin_protects_target(tmp_path, monkeypatch):
    """DQ.9: a pinned target survives even when sweep would otherwise evict it."""
    import client as client_mod  # noqa: PLC0415

    cache = tmp_path / "cache" / "stem-a"
    cache.mkdir(parents=True)
    (cache / "blob").write_bytes(b"\x00" * 5000)

    monkeypatch.setattr(client_mod, "_ESPHOME_VERSIONS_DIR", str(tmp_path))
    prev = client_mod._get_current_disk_quota_bytes()
    try:
        client_mod._set_current_disk_quota_bytes(100)  # very tight
        with client_mod._active_job_set.pin("2026.4.3", "stem-a", 1):
            client_mod._run_disk_quota_sweep(label="test")
            assert (tmp_path / "cache" / "stem-a").exists()
    finally:
        client_mod._set_current_disk_quota_bytes(prev)


def test_default_constructed_version_manager_collapses_to_one(tmp_path):
    """DQ.8: VersionManager() (no override) keeps at most one venv."""
    _add_fake_version(tmp_path, "2026.4.1")
    _add_fake_version(tmp_path, "2026.4.2")
    _add_fake_version(tmp_path, "2026.4.3")

    vm_one = VersionManager(versions_base=tmp_path)
    # The constructor's _load_existing accepted all 3 from disk; after the
    # first ensure_version-style call, the count cap kicks in. Direct
    # eviction call to verify the cap is 1.
    assert len(vm_one.installed_versions()) == 3
    # Force eviction via the internal helper used by ensure_version.
    while len(vm_one.installed_versions()) > vm_one._max_versions:
        with vm_one._lock:
            vm_one._evict_lru(keep_version=None)
    assert len(vm_one.installed_versions()) == 1
