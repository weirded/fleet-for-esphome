"""DQ.7 — disk-quota engine for the worker.

Bounds disk usage under ``/esphome-versions/`` to a single byte budget.
Replaces the older "max-N venvs + min-free-disk-%" pair of knobs with a
unified mtime-LRU eviction policy that spans every category of cache the
worker keeps.

Layout under ``base = /esphome-versions/``::

    <version>/         ESPHome venvs (e.g. "2026.4.3/" — has bin/esphome)
    cache/<stem>/      shared per-target compile cache (.pio + .esphome)
    slots/<N>/<stem>/  per-slot per-target working dir
    pio-slot-<N>/      per-slot PlatformIO core dir (~500 MB toolchain)
    .client_id         file (ignored)
    .platformio        dir (ignored — host PIO state, not our cache)

Eviction policy (cheapest → most expensive to recreate):

1. **Orphan slot dirs** (``slots/<N>/`` + ``pio-slot-<N>/`` where
   ``N >= max_slots``) — :func:`prune_orphans` runs unconditionally first;
   these can never be in flight because the worker only spawns slot ids
   ``1..max_slots``.
2. **Stale ESPHome venvs** — collapse to 1 (most recently used).
   ~1–3 min to re-``pip install``.
3. **Per-target caches** (``cache/<stem>/`` plus every
   ``slots/*/<stem>/`` for the same stem, evicted as a unit) —
   mtime-LRU. 3–5 min cold rebuild.
4. **PlatformIO toolchains** (whole ``pio-slot-<N>/`` dirs) — last
   resort, mtime-LRU. 5–10 min re-extract.

**Pinning** (must-not-evict): the venv, ``slots/<N>/<stem>/``,
``pio-slot-<N>/``, and ``cache/<stem>/`` of any in-flight job.
:class:`ActiveJobSet` tracks the in-memory active set; its
:meth:`snapshot` returns a :class:`PinnedSet` callers pass to
:func:`enforce_quota` and :func:`host_disk_floor`.

When only pinned items remain and usage still exceeds the quota the
engine logs a warning and returns without evicting — interrupting a
live build to free disk is worse than letting usage briefly exceed the
budget. The next post-job sweep will catch up.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Byte breakdown of ``base`` by category."""

    venv_bytes: int = 0
    cache_bytes: int = 0
    slot_bytes: int = 0
    pio_slot_bytes: int = 0
    other_bytes: int = 0
    truncated: bool = False  # #144: set when a deadline-bounded walk gave up early

    @property
    def total_bytes(self) -> int:
        return (
            self.venv_bytes
            + self.cache_bytes
            + self.slot_bytes
            + self.pio_slot_bytes
            + self.other_bytes
        )


@dataclass
class SweepResult:
    """What a single sweep freed, broken down by category."""

    freed_bytes: int = 0
    orphan_slots_evicted: int = 0
    venvs_evicted: int = 0
    targets_evicted: int = 0
    pio_slots_evicted: int = 0


@dataclass
class PinnedSet:
    """Snapshot of dirs that must not be evicted while a job is running."""

    venv_versions: set[str] = field(default_factory=set)
    target_stems: set[str] = field(default_factory=set)
    slot_ids: set[int] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Pinning context
# ---------------------------------------------------------------------------


class ActiveJobSet:
    """Thread-safe registry of in-flight job pins.

    Each running job calls :meth:`pin` with its venv version, target stem,
    and slot id; the context manager unpins on exit. :meth:`snapshot`
    returns a :class:`PinnedSet` of every dir that's currently in use
    across all jobs (refcounted so two jobs on the same venv are both
    protected until both finish).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._venvs: dict[str, int] = {}
        self._stems: dict[str, int] = {}
        self._slots: dict[int, int] = {}

    @contextmanager
    def pin(self, version: str, target_stem: str, slot_id: int) -> Iterator[None]:
        with self._lock:
            self._venvs[version] = self._venvs.get(version, 0) + 1
            self._stems[target_stem] = self._stems.get(target_stem, 0) + 1
            self._slots[slot_id] = self._slots.get(slot_id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._venvs[version] -= 1
                if self._venvs[version] <= 0:
                    del self._venvs[version]
                self._stems[target_stem] -= 1
                if self._stems[target_stem] <= 0:
                    del self._stems[target_stem]
                self._slots[slot_id] -= 1
                if self._slots[slot_id] <= 0:
                    del self._slots[slot_id]

    def snapshot(self) -> PinnedSet:
        with self._lock:
            return PinnedSet(
                venv_versions=set(self._venvs.keys()),
                target_stems=set(self._stems.keys()),
                slot_ids=set(self._slots.keys()),
            )


# ---------------------------------------------------------------------------
# Filesystem helpers (best-effort; missing files are skipped)
# ---------------------------------------------------------------------------


def _du_bytes(path: Path, deadline: Optional[float] = None) -> int:
    """Recursive sum of file sizes under ``path``.

    ``deadline`` is a :func:`time.monotonic` timestamp; when set and
    exceeded, the walk stops early and returns whatever has been summed
    so far. The caller (:func:`compute_usage`) detects truncation by
    checking the clock after the walk and surfaces it via
    :attr:`Usage.truncated`. Bounding the walk this way keeps slow /
    NFS-mounted bases from parking the heartbeat thread (#144).
    """
    total = 0
    try:
        for entry in os.scandir(path):
            if deadline is not None and time.monotonic() > deadline:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    total += _du_bytes(Path(entry.path), deadline=deadline)
                elif entry.is_file(follow_symlinks=False):
                    try:
                        total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
            except OSError:
                pass
    except (OSError, FileNotFoundError):
        pass
    return total


def _is_venv_dir(path: Path) -> bool:
    return (path / "bin" / "esphome").exists()


def _list_venv_dirs(base: Path) -> list[tuple[str, Path, float]]:
    """Return ``[(version, path, mtime)]`` for every venv, oldest first."""
    out: list[tuple[str, Path, float]] = []
    if not base.exists():
        return out
    for entry in base.iterdir():
        if entry.is_dir() and _is_venv_dir(entry):
            try:
                out.append((entry.name, entry, entry.stat().st_mtime))
            except OSError:
                continue
    out.sort(key=lambda t: t[2])
    return out


def _list_slot_ids(base: Path) -> list[int]:
    """Slot ids that have either ``slots/<N>/`` or ``pio-slot-N/`` on disk."""
    seen: set[int] = set()
    slots_dir = base / "slots"
    if slots_dir.is_dir():
        for entry in slots_dir.iterdir():
            try:
                seen.add(int(entry.name))
            except ValueError:
                continue
    if base.exists():
        for entry in base.iterdir():
            if entry.is_dir() and entry.name.startswith("pio-slot-"):
                try:
                    seen.add(int(entry.name[len("pio-slot-"):]))
                except ValueError:
                    continue
    return sorted(seen)


def _list_target_stems(base: Path) -> list[tuple[str, float]]:
    """Per-target stems from ``cache/<stem>/``, oldest first by cache mtime."""
    out: list[tuple[str, float]] = []
    cache_dir = base / "cache"
    if not cache_dir.is_dir():
        return out
    for entry in cache_dir.iterdir():
        if entry.is_dir():
            try:
                out.append((entry.name, entry.stat().st_mtime))
            except OSError:
                continue
    out.sort(key=lambda t: t[1])
    return out


def _target_dirs(base: Path, stem: str) -> list[Path]:
    """Every dir on disk that belongs to a single target stem.

    Includes ``cache/<stem>/`` and every ``slots/<N>/<stem>/``. Eviction
    of a target evicts the whole unit so the cache lock semantics stay
    consistent with the worker's ``_sync_slot_into_cache`` flow.
    """
    dirs: list[Path] = []
    cache_dir = base / "cache" / stem
    if cache_dir.exists():
        dirs.append(cache_dir)
    slots_dir = base / "slots"
    if slots_dir.is_dir():
        for slot_entry in slots_dir.iterdir():
            cand = slot_entry / stem
            if cand.exists():
                dirs.append(cand)
    return dirs


def _list_pio_slots(base: Path) -> list[tuple[int, Path, float]]:
    """``[(slot_id, path, mtime)]`` for every ``pio-slot-N/`` dir, oldest first."""
    out: list[tuple[int, Path, float]] = []
    if not base.exists():
        return out
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.startswith("pio-slot-"):
            try:
                sid = int(entry.name[len("pio-slot-"):])
            except ValueError:
                continue
            try:
                out.append((sid, entry, entry.stat().st_mtime))
            except OSError:
                continue
    out.sort(key=lambda t: t[2])
    return out


def _rmtree(path: Path) -> int:
    """Best-effort recursive delete; returns bytes freed (0 on failure)."""
    if not path.exists():
        return 0
    freed = _du_bytes(path)
    try:
        shutil.rmtree(str(path))
    except OSError as exc:
        logger.warning("disk-quota: failed to remove %s: %s", path, exc)
        return 0
    return freed


# ---------------------------------------------------------------------------
# Public engine API
# ---------------------------------------------------------------------------


def compute_usage(base: Path, deadline_s: Optional[float] = None) -> Usage:
    """Walk ``base`` once, attributing bytes to a category.

    ``deadline_s`` (seconds) caps the wall-clock cost of the walk; when
    exceeded, returns a partial :class:`Usage` with ``truncated=True``.
    Eviction-loop callers (:func:`enforce_quota`'s ``should_stop``) must
    pass ``None`` so the comparison against the quota stays accurate;
    sampling/diagnostic callers should pass a budget so a slow ``base``
    can't park the calling thread (#144).
    """
    u = Usage()
    if not base.exists():
        return u
    deadline = (time.monotonic() + deadline_s) if deadline_s is not None else None
    for entry in base.iterdir():
        if deadline is not None and time.monotonic() > deadline:
            u.truncated = True
            return u
        try:
            if entry.is_file():
                try:
                    u.other_bytes += entry.stat().st_size
                except OSError:
                    pass
                continue
            if not entry.is_dir():
                continue
        except OSError:
            continue
        name = entry.name
        if _is_venv_dir(entry):
            u.venv_bytes += _du_bytes(entry, deadline=deadline)
        elif name == "cache":
            u.cache_bytes += _du_bytes(entry, deadline=deadline)
        elif name == "slots":
            u.slot_bytes += _du_bytes(entry, deadline=deadline)
        elif name.startswith("pio-slot-"):
            u.pio_slot_bytes += _du_bytes(entry, deadline=deadline)
        else:
            u.other_bytes += _du_bytes(entry, deadline=deadline)
    if deadline is not None and time.monotonic() > deadline:
        u.truncated = True
    return u


def prune_orphans(base: Path, max_slots: int) -> SweepResult:
    """Remove ``slots/<N>/`` + ``pio-slot-N/`` for ``N > max_slots``.

    Worker thread ids are ``1..max_slots`` (1-indexed; see
    ``client.start_workers`` → ``args=(i + 1, ...)``). Slots in that
    range can be in flight; only ids strictly above ``max_slots`` are
    orphaned by a downsizing of ``MAX_PARALLEL_JOBS`` and safe to wipe
    unconditionally.
    """
    result = SweepResult()
    for slot_id in _list_slot_ids(base):
        if slot_id <= max_slots:
            continue
        slot_dir = base / "slots" / str(slot_id)
        pio_dir = base / f"pio-slot-{slot_id}"
        slot_existed = slot_dir.exists()
        pio_existed = pio_dir.exists()
        freed = _rmtree(slot_dir) + _rmtree(pio_dir)
        if slot_existed or pio_existed:
            result.freed_bytes += freed
            result.orphan_slots_evicted += 1
    return result


def _evict_until(
    base: Path,
    *,
    pinned: PinnedSet,
    should_stop,
    result: SweepResult,
) -> None:
    """Evict in policy order until ``should_stop()`` returns True.

    ``should_stop`` is re-checked after every eviction unit (one venv,
    one target, one pio-slot). Used by both :func:`enforce_quota` (stop
    when total bytes ≤ quota) and :func:`host_disk_floor` (stop when
    host free% ≥ threshold).

    Order: stale venvs (collapse to 1, MRU kept) → per-target caches
    (mtime-LRU) → pio-slot toolchains (mtime-LRU). Pinned items are
    skipped. When only pinned items remain the loop exits without
    eviction (caller logs).
    """
    if should_stop():
        return

    # Step 1: collapse venvs to 1 (most recently used). Skip pinned.
    venvs = _list_venv_dirs(base)
    keep_idx = len(venvs) - 1  # MRU = last after oldest-first sort
    for i, (ver, path, _mtime) in enumerate(venvs):
        if i == keep_idx:
            continue
        if ver in pinned.venv_versions:
            continue
        if should_stop():
            return
        freed = _rmtree(path)
        if freed > 0:
            result.freed_bytes += freed
            result.venvs_evicted += 1

    if should_stop():
        return

    # Step 2: per-target caches (oldest first), evicted as a unit.
    for stem, _mtime in _list_target_stems(base):
        if stem in pinned.target_stems:
            continue
        if should_stop():
            return
        freed = 0
        for d in _target_dirs(base, stem):
            freed += _rmtree(d)
        if freed > 0:
            result.freed_bytes += freed
            result.targets_evicted += 1

    if should_stop():
        return

    # Step 3: pio-slot toolchains (mtime-LRU). Last resort.
    for sid, path, _mtime in _list_pio_slots(base):
        if sid in pinned.slot_ids:
            continue
        if should_stop():
            return
        freed = _rmtree(path)
        if freed > 0:
            result.freed_bytes += freed
            result.pio_slots_evicted += 1


def enforce_quota(
    base: Path,
    quota_bytes: int,
    *,
    pinned: PinnedSet,
) -> SweepResult:
    """Evict in policy order until total usage ≤ ``quota_bytes``.

    Idempotent: a second call on a steady-state tree is a no-op.
    Orphan slot dirs are pruned by :func:`prune_orphans` separately —
    callers normally invoke that first.
    """
    result = SweepResult()
    if not base.exists():
        return result

    def should_stop() -> bool:
        return compute_usage(base).total_bytes <= quota_bytes

    _evict_until(base, pinned=pinned, should_stop=should_stop, result=result)

    if not should_stop():
        logger.warning(
            "disk-quota: usage %d B exceeds quota %d B but only pinned "
            "items remain — letting it ride until a job finishes",
            compute_usage(base).total_bytes, quota_bytes,
        )
    return result


def host_disk_floor(
    base: Path,
    min_free_pct: int,
    *,
    pinned: PinnedSet,
) -> SweepResult:
    """Emergency override: evict beyond the quota when host free% drops low.

    Workers share the disk with HA core, Docker, and logs. This kicks in
    when external pressure (not our usage) tips host disk into the danger
    zone. Eviction order matches :func:`enforce_quota`; the loop stops the
    moment free% is back above ``min_free_pct``.
    """
    result = SweepResult()
    if not base.exists():
        return result

    def free_pct() -> Optional[float]:
        try:
            st = os.statvfs(str(base))
            total = st.f_frsize * st.f_blocks
            free = st.f_frsize * st.f_bavail
            return (free / total) * 100 if total > 0 else None
        except OSError:
            return None

    pct = free_pct()
    if pct is None or pct >= min_free_pct:
        return result

    def should_stop() -> bool:
        p = free_pct()
        return p is None or p >= min_free_pct

    _evict_until(base, pinned=pinned, should_stop=should_stop, result=result)
    return result
