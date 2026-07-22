"""#242: shared ESP-IDF cache install race + self-heal.

ESPHome installs the ESP-IDF framework/toolchain into a process-global
cache and does no locking around it. Running MAX_PARALLEL_JOBS esphome
processes against that one cache lets a cold install be raced, which
leaves a partial tree that ESPHome never repairs — every later job then
fails in under a second with "framework installation failure".

These tests cover the two halves of the fix: serialising the cold
install, and healing a cache that is already poisoned.
"""
from __future__ import annotations

import multiprocessing
import os
import time

# client.py reads SERVER_URL / SERVER_TOKEN at import time. Set before the
# import here and in the spawned child of the lock test (which inherits
# this process's environment).
os.environ.setdefault("SERVER_URL", "http://127.0.0.1:1")
os.environ.setdefault("SERVER_TOKEN", "test-token")

import client  # noqa: E402


# ---------------------------------------------------------------------------
# Signature detection
# ---------------------------------------------------------------------------

def test_detects_framework_installation_failure():
    """The dominant real-world signature — ai-macbook, 2026.7.1/IDF 5.5.5."""
    log = (
        '  File ".../esphome/espidf/framework.py", line 829, in '
        "_check_esphome_idf_framework_install\n"
        '    raise RuntimeError(f"ESP-IDF {version} framework installation failure")\n'
        "RuntimeError: ESP-IDF 5.5.5 framework installation failure\n"
    )
    assert client._is_broken_idf_state(log)


def test_detects_missing_dynconfig():
    """docker-optiplex-5's flavour: toolchain without its dynconfig .so files."""
    log = (
        "thread 'main' panicked at main.rs:144:5:\n"
        "Dynconfig for target esp32s3 is not exist "
        "(/root/.cache/esphome/idf/tools/xtensa-esp-elf/esp-14.2.0_20260121/"
        "xtensa-esp-elf/lib/xtensa_esp32s3.so)\n"
    )
    assert client._is_broken_idf_state(log)


def test_detects_missing_multilib():
    """Toolchain missing the newlib archives the gcc driver searches."""
    log = "ld: cannot find -lnosys: No such file or directory\n"
    assert client._is_broken_idf_state(log)


def test_healthy_log_is_not_flagged():
    """A normal successful compile must not trigger a cache wipe."""
    log = "INFO Successfully compiled program.\nINFO Successfully uploaded program.\n"
    assert not client._is_broken_idf_state(log)


def test_unrelated_failure_is_not_flagged():
    """A YAML error is not IDF corruption — wiping a 4 GiB cache for it
    would turn a 2-second user mistake into a 10-minute re-download."""
    log = "ERROR Error while reading config: Component not found: nonexistent_sensor\n"
    assert not client._is_broken_idf_state(log)


# ---------------------------------------------------------------------------
# Warm marker
# ---------------------------------------------------------------------------

def _populate_cache(root: str, idf_version: str = "5.5.5") -> None:
    """Create a cache that looks completely installed."""
    os.makedirs(os.path.join(root, "frameworks", idf_version, "tools", "cmake"), exist_ok=True)


def test_cold_cache_is_not_warm(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert not client._idf_is_warm("2026.7.1")


def test_marking_warm_requires_a_populated_cache(tmp_path, monkeypatch):
    """Regression guard for the false-positive that would reintroduce the bug.

    A compile can fail before ESPHome ever touches the IDF cache (bad
    YAML, missing secret). Marking that as warm would let the next batch
    race a still-cold cache.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    client._mark_idf_warm("2026.7.1")
    assert not client._idf_is_warm("2026.7.1"), (
        "empty cache must not be recorded as warm"
    )


def test_marking_warm_succeeds_once_populated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _populate_cache(os.path.join(str(tmp_path), "esphome", "idf"))
    client._mark_idf_warm("2026.7.1")
    assert client._idf_is_warm("2026.7.1")


def test_partial_framework_is_not_considered_populated(tmp_path, monkeypatch):
    """The exact observed corruption: framework dir present, tools/ absent.

    This is what ESPHome accepts as "installed" and then fails on forever.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    root = os.path.join(str(tmp_path), "esphome", "idf")
    os.makedirs(os.path.join(root, "frameworks", "5.5.5", "components"))
    assert not client._idf_cache_looks_populated()
    client._mark_idf_warm("2026.7.1")
    assert not client._idf_is_warm("2026.7.1")


def test_warm_marker_lives_inside_the_cache(tmp_path, monkeypatch):
    """The marker must die with the cache it describes.

    The IDF cache lives in the worker container's writable layer, so a
    container recreate wipes it. A marker stored on the persistent
    /esphome-versions volume would survive and wrongly report the fresh
    cold cache as warm — which is exactly how a reinstall re-triggered
    the race.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    marker = client._idf_warm_marker("2026.7.1")
    assert marker.startswith(client._idf_cache_root())


# ---------------------------------------------------------------------------
# Self-heal wipe
# ---------------------------------------------------------------------------

def test_wipe_removes_corrupted_subtrees(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    root = os.path.join(str(tmp_path), "esphome", "idf")
    _populate_cache(root)
    os.makedirs(os.path.join(root, "tools", "xtensa-esp-elf"))

    assert client._wipe_broken_idf_cache("2026.7.1")
    assert not os.path.exists(os.path.join(root, "frameworks"))
    assert not os.path.exists(os.path.join(root, "tools"))


def test_wipe_clears_warm_markers(tmp_path, monkeypatch):
    """The markers described the cache we just deleted; leaving them would
    send the next compile down the shared-lock path against a cold cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    root = os.path.join(str(tmp_path), "esphome", "idf")
    _populate_cache(root)
    client._mark_idf_warm("2026.7.1")
    assert client._idf_is_warm("2026.7.1")

    client._wipe_broken_idf_cache("2026.7.1")
    assert not client._idf_is_warm("2026.7.1")


def test_wipe_on_empty_cache_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert not client._wipe_broken_idf_cache("2026.7.1")


def test_wipe_refuses_when_disk_is_low(tmp_path, monkeypatch):
    """#219 precedent: wiping only to hit ENOSPC mid-re-extract would
    re-corrupt exactly what we just cleaned — a guaranteed loop."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    root = os.path.join(str(tmp_path), "esphome", "idf")
    _populate_cache(root)

    class _Usage:
        free = 1 * 1024 ** 3  # 1 GiB — below the 5 GiB floor

    monkeypatch.setattr(client.shutil, "disk_usage", lambda _p: _Usage())
    assert not client._wipe_broken_idf_cache("2026.7.1")
    assert os.path.exists(os.path.join(root, "frameworks")), (
        "cache must be left intact when there is no room to rebuild it"
    )


# ---------------------------------------------------------------------------
# The lock itself — the actual race
# ---------------------------------------------------------------------------

def _hold_exclusive(versions_dir: str, started, hold_s: float) -> None:
    client._ESPHOME_VERSIONS_DIR = versions_dir
    with client._idf_install_lock(exclusive=True):
        started.set()
        time.sleep(hold_s)


def test_exclusive_lock_blocks_a_second_slot(tmp_path, monkeypatch):
    """The core regression test.

    Reproduces the production trigger: two build slots reaching a cold
    IDF cache at the same instant. Pre-fix both proceeded and corrupted
    the tree; post-fix the second must wait for the first to finish.

    Uses real processes, not threads — fcntl.flock is per-open-file-
    description, so a threaded version would not exercise the same
    exclusion the worker's separate esphome processes rely on.
    """
    versions_dir = str(tmp_path / "versions")
    os.makedirs(versions_dir, exist_ok=True)
    monkeypatch.setattr(client, "_ESPHOME_VERSIONS_DIR", versions_dir)

    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    hold_s = 1.0
    holder = ctx.Process(target=_hold_exclusive, args=(versions_dir, started, hold_s))
    holder.start()
    try:
        assert started.wait(timeout=10), "holder never acquired the lock"
        t0 = time.monotonic()
        with client._idf_install_lock(exclusive=True):
            waited = time.monotonic() - t0
        assert waited >= hold_s * 0.5, (
            f"second slot acquired the lock after only {waited:.2f}s — "
            "the cold IDF install is not serialised"
        )
    finally:
        holder.join(timeout=10)


def test_shared_locks_do_not_block_each_other(tmp_path, monkeypatch):
    """Warm-cache compiles must stay parallel — serialising every compile
    would cost far more than the bug does."""
    versions_dir = str(tmp_path / "versions")
    os.makedirs(versions_dir, exist_ok=True)
    monkeypatch.setattr(client, "_ESPHOME_VERSIONS_DIR", versions_dir)

    t0 = time.monotonic()
    with client._idf_install_lock(exclusive=False):
        with client._idf_install_lock(exclusive=False):
            pass
    assert time.monotonic() - t0 < 5.0


def test_lock_failure_degrades_gracefully(tmp_path, monkeypatch):
    """An unlockable filesystem should fall back to today's behaviour
    rather than refuse to compile."""
    monkeypatch.setattr(client, "_ESPHOME_VERSIONS_DIR", str(tmp_path))

    def _boom(*_a, **_kw):
        raise OSError("no locks available")

    monkeypatch.setattr(client.fcntl, "flock", _boom)
    with client._idf_install_lock(exclusive=True):
        pass  # must not raise
