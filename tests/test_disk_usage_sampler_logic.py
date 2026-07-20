"""Regression tests for #144 — heartbeat must not block on disk_usage walks.

Pre-fix, ``_build_system_info`` called ``disk_quota.compute_usage`` inline
on every heartbeat. On standalone-Docker deployments with slow ``/data``
storage (NAS / NFS / spinning disk / overlay-fs), the walk took longer
than the server's 30 s offline threshold, the worker flipped offline,
and any in-flight job got abandoned at ~20 s with
``reason=only_online_worker``.

The fix moves the walk onto a background sampler thread with a
wall-clock budget; ``_build_system_info`` reads from the sampler's
cache. These tests pin both halves:

1. :func:`disk_quota.compute_usage` honours its ``deadline_s`` budget
   (returns ``Usage.truncated=True`` instead of running over).
2. ``client._build_system_info`` reads from
   ``client._record_disk_usage_sample``'s cache and never walks the
   tree itself, so even an artificially slow ``compute_usage`` doesn't
   block heartbeat assembly.

PY-10 ``_logic`` suffix: pure-logic, no real HA, no real ESPHome
install, no real network — every test is deterministic.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

# client.py reads SERVER_URL / SERVER_TOKEN at import time.
os.environ.setdefault("SERVER_URL", "http://127.0.0.1:1")
os.environ.setdefault("SERVER_TOKEN", "test-token")

import disk_quota  # noqa: E402
from disk_quota import Usage, compute_usage  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic-tree builder — small, just enough to exercise the deadline path.
# ---------------------------------------------------------------------------


def _make_venv(base: Path, version: str, *, total_size: int) -> Path:
    vdir = base / version
    (vdir / "bin").mkdir(parents=True, exist_ok=True)
    (vdir / "bin" / "esphome").write_bytes(b"")
    (vdir / "lib").mkdir(parents=True, exist_ok=True)
    (vdir / "lib" / "padding").write_bytes(b"\x00" * total_size)
    return vdir


# ---------------------------------------------------------------------------
# 1. compute_usage honours deadline_s
# ---------------------------------------------------------------------------


def test_compute_usage_no_deadline_walks_full_tree(tmp_path: Path) -> None:
    """Sanity: with deadline_s=None, behaviour is unchanged from pre-#144."""
    _make_venv(tmp_path, "2026.4.3", total_size=1000)
    u = compute_usage(tmp_path)
    assert u.venv_bytes == 1000
    assert u.truncated is False


def test_compute_usage_truncates_when_walk_exceeds_budget(tmp_path: Path) -> None:
    """Slow ``os.scandir`` (simulated) trips the deadline → truncated=True.

    This is the regression on #144's failure mode: pre-fix, ``_du_bytes``
    had no time bound, so a slow ``base`` parked the heartbeat thread
    indefinitely. With ``deadline_s`` set, the walk gives up early and
    returns whatever it has summed so far.
    """
    _make_venv(tmp_path, "2026.4.3", total_size=1000)
    _make_venv(tmp_path, "2026.5.0", total_size=2000)

    # Simulate slow storage by sleeping inside the recursive helper.
    real_du = disk_quota._du_bytes
    call_count = {"n": 0}

    def slow_du(path: Path, deadline=None) -> int:
        call_count["n"] += 1
        # Sleep enough to blow the budget within a few iterations.
        time.sleep(0.05)
        return real_du(path, deadline=deadline)

    with patch.object(disk_quota, "_du_bytes", side_effect=slow_du):
        u = compute_usage(tmp_path, deadline_s=0.05)

    assert u.truncated is True
    assert call_count["n"] >= 1  # walk did start
    # Partial total may be 0 (if the very first dir tripped the budget) or
    # positive (if one venv summed before the budget ran out). Both are
    # honest partial states — we only assert the truncation flag and that
    # we returned at all (which we did, otherwise pytest would hang).


def test_compute_usage_zero_budget_returns_immediately(tmp_path: Path) -> None:
    """``deadline_s=0`` → walk gives up before processing any entry."""
    _make_venv(tmp_path, "2026.4.3", total_size=1000)
    u = compute_usage(tmp_path, deadline_s=0.0)
    assert u.truncated is True
    # Some implementations will catch the deadline before iter starts (total=0)
    # and some will sum the first entry then trip — both honest. The contract
    # is: truncated=True, no exception, no infinite loop.
    assert u.total_bytes >= 0


# ---------------------------------------------------------------------------
# 2. _build_system_info reads from sample cache, never walks
# ---------------------------------------------------------------------------


def test_build_system_info_reads_from_cache_not_compute_usage(tmp_path: Path) -> None:
    """``_build_system_info`` must not call ``disk_quota.compute_usage``.

    Pre-fix it called it inline on every heartbeat, so a slow walk
    blocked heartbeat assembly. Post-fix it reads from
    ``_disk_usage_sample_bytes``, populated by the background sampler
    thread.
    """
    import client as client_mod  # noqa: PLC0415

    # Seed the sample cache with a known value, the way the sampler would.
    client_mod._record_disk_usage_sample(total_bytes=12345, truncated=False)

    # Patch ``compute_usage`` to a hard-fail — if heartbeat assembly walks
    # the tree, the test fails immediately. Pre-fix this would have raised.
    with patch.object(disk_quota, "compute_usage", side_effect=AssertionError(
        "_build_system_info must not call compute_usage on the heartbeat path",
    )):
        info = client_mod._build_system_info()

    assert info.disk_usage_bytes == 12345


def test_build_system_info_omits_disk_usage_when_cache_empty() -> None:
    """First-boot heartbeat (sampler hasn't run yet) omits ``disk_usage_bytes``.

    Server tolerates the absent field — it falls back to last-known or
    null. Better than blocking heartbeat to produce a synchronous value.
    """
    import client as client_mod  # noqa: PLC0415

    # Reset cache to "not yet measured".
    with client_mod._disk_usage_sample_lock:
        client_mod._disk_usage_sample_bytes = None

    info = client_mod._build_system_info()
    # SystemInfo's disk_usage_bytes is Optional; absent → None / not set.
    assert info.disk_usage_bytes is None


def test_record_disk_usage_sample_round_trip() -> None:
    """Sanity: writes via ``_record_disk_usage_sample`` are visible via the getter."""
    import client as client_mod  # noqa: PLC0415

    client_mod._record_disk_usage_sample(total_bytes=999, truncated=True)
    assert client_mod._get_disk_usage_sample() == 999
    with client_mod._disk_usage_sample_lock:
        assert client_mod._disk_usage_sample_truncated is True
