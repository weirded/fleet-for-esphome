"""Build worker registry — in-memory, mirrored to ``/data/workers.json``.

Lifecycle rule: a worker stays in the registry until the operator deletes
it. Neither a clean worker shutdown (``mark_stopped``) nor an add-on
restart (``load``) drops an entry — both just leave it offline until the
next heartbeat.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
# Heartbeats arrive every ~10 s per worker; persisting each one would be
# wasteful. ``last_seen`` is flushed at most this often so the "offline
# for" display survives a crash without a per-heartbeat disk write.
_HEARTBEAT_SAVE_INTERVAL_SECS = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _broadcast_workers_changed() -> None:
    """Fire an event on the server event bus (#41). No-op on import error."""
    try:
        from event_bus import EVENT_WORKERS_CHANGED, broadcast  # noqa: PLC0415
        broadcast(EVENT_WORKERS_CHANGED)
    except Exception:
        logger.debug("event_bus broadcast failed", exc_info=True)


@dataclass
class Worker:
    client_id: str
    hostname: str
    platform: str
    last_seen: datetime = field(default_factory=_utcnow)
    current_job_id: Optional[str] = None
    disabled: bool = False
    client_version: Optional[str] = None
    image_version: Optional[str] = None  # baked-in Docker image version (separate from client_version, which is source code)
    max_parallel_jobs: int = 1
    requested_max_parallel_jobs: Optional[int] = None  # set via UI, pushed in heartbeat
    pending_clean: bool = False  # set via UI, pushed in heartbeat
    system_info: Optional[dict] = None
    # #219: self-imposed claim block when the worker's heartbeat reports
    # disk_used_pct at/above the enter threshold. Distinct from ``disabled``
    # (which is a sticky operator choice) — this auto-resumes the moment
    # the worker reports it's back below the exit threshold.
    health_blocked_reason: Optional[str] = None
    # TG.1: user-managed tags. Resolved at registration time by the
    # WorkerTagStore (hostname, falling back to client_id, is the identity);
    # this in-memory copy is kept in sync so UI reads off the registry don't
    # have to round-trip the disk store.
    tags: list[str] = field(default_factory=list)
    # DQ.3: per-worker disk-quota override in bytes (None = inherit
    # AppSettings.default_worker_disk_quota_bytes). Resolved at registration
    # time by the WorkerDiskQuotaStore (same hostname/client_id identity as
    # tags) and kept in sync via UI edit; mirrored in-memory here so
    # /ui/api/workers responses don't round-trip the disk store on every
    # request. Use ``effective_disk_quota_bytes(default)`` to resolve the
    # value the worker should actually enforce against.
    disk_quota_bytes: Optional[int] = None
    # Set by a clean worker shutdown (deregister). Forces ``is_online`` to
    # False immediately instead of waiting out the offline threshold, and
    # clears on the next heartbeat / registration. The entry itself stays.
    stopped: bool = False

    def effective_disk_quota_bytes(self, default_bytes: int) -> int:
        """Return the override if set, else the supplied fleet default."""
        return self.disk_quota_bytes if self.disk_quota_bytes is not None else default_bytes

    def to_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "hostname": self.hostname,
            "platform": self.platform,
            "last_seen": self.last_seen.isoformat(),
            "current_job_id": self.current_job_id,
            "disabled": self.disabled,
            "client_version": self.client_version,
            "image_version": self.image_version,
            "max_parallel_jobs": self.max_parallel_jobs,
            "requested_max_parallel_jobs": self.requested_max_parallel_jobs,
            "pending_clean": self.pending_clean,
            "system_info": self.system_info,
            "health_blocked_reason": self.health_blocked_reason,
            "tags": list(self.tags),
            # DQ.5: persisted override (may be null = inherit fleet default).
            # The effective value is computed in the UI API layer where the
            # fleet default is in scope.
            "disk_quota_override_bytes": self.disk_quota_bytes,
        }

    def to_persist_dict(self) -> dict:
        """Durable subset of the worker: everything except in-flight job state."""
        return {
            "client_id": self.client_id,
            "hostname": self.hostname,
            "platform": self.platform,
            "last_seen": self.last_seen.isoformat(),
            "disabled": self.disabled,
            "client_version": self.client_version,
            "image_version": self.image_version,
            "max_parallel_jobs": self.max_parallel_jobs,
            "requested_max_parallel_jobs": self.requested_max_parallel_jobs,
            "pending_clean": self.pending_clean,
            "system_info": self.system_info,
            "tags": list(self.tags),
            "disk_quota_bytes": self.disk_quota_bytes,
            "stopped": self.stopped,
        }

    @classmethod
    def from_persist_dict(cls, d: dict) -> "Worker":
        """Inverse of ``to_persist_dict``. Raises on a malformed entry."""
        last_seen_raw = d.get("last_seen")
        last_seen = datetime.fromisoformat(last_seen_raw) if isinstance(last_seen_raw, str) else _utcnow()
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        tags = d.get("tags")
        return cls(
            client_id=str(d["client_id"]),
            hostname=str(d.get("hostname") or ""),
            platform=str(d.get("platform") or ""),
            last_seen=last_seen,
            disabled=bool(d.get("disabled", False)),
            client_version=d.get("client_version"),
            image_version=d.get("image_version"),
            max_parallel_jobs=int(d.get("max_parallel_jobs", 1)),
            requested_max_parallel_jobs=d.get("requested_max_parallel_jobs"),
            pending_clean=bool(d.get("pending_clean", False)),
            system_info=d.get("system_info") if isinstance(d.get("system_info"), dict) else None,
            tags=[t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
            disk_quota_bytes=d.get("disk_quota_bytes"),
            stopped=bool(d.get("stopped", False)),
        )

    def evaluate_health(self) -> bool:
        """Recompute ``health_blocked_reason`` from ``system_info`` (#219, + absolute-floor fix).

        Enter blocked only when BOTH signals are bad: ``disk_used_pct`` at/above
        ``WORKER_DISK_BLOCK_ENTER_PCT`` AND absolute free space below
        ``WORKER_DISK_FREE_FLOOR_BYTES``. A many-TB volume that's "95% full" but
        has 1+ TB free must NOT trip this — the app only ever needs ~1.6 GB.
        Exit (clear) on EITHER signal recovering: usage drops to/below
        ``WORKER_DISK_BLOCK_EXIT_PCT``, OR absolute free rises back above the
        floor. Deliberately asymmetric (AND to enter, OR to exit) so the gate
        stays conservative about newly blocking but quick to unblock.

        Backward compatibility: if the worker hasn't upgraded to send
        ``disk_free_bytes`` (or sends a non-numeric value), falls back to the
        original pure-percentage hysteresis unchanged.

        Returns True iff the state transitioned (caller can broadcast).
        """
        from constants import (  # noqa: PLC0415
            WORKER_DISK_BLOCK_ENTER_PCT,
            WORKER_DISK_BLOCK_EXIT_PCT,
            WORKER_DISK_FREE_FLOOR_BYTES,
        )
        info = self.system_info or {}
        pct = info.get("disk_used_pct")
        if pct is None:
            return False
        try:
            pct_int = int(pct)
        except (TypeError, ValueError):
            return False

        free_bytes_raw = info.get("disk_free_bytes")
        free_bytes: Optional[int] = None
        if free_bytes_raw is not None:
            try:
                free_bytes = int(free_bytes_raw)
            except (TypeError, ValueError):
                free_bytes = None

        previous = self.health_blocked_reason

        if free_bytes is None:
            # Old worker / missing field: percentage-only, exactly as before.
            if previous is None and pct_int >= WORKER_DISK_BLOCK_ENTER_PCT:
                self.health_blocked_reason = "disk_full"
            elif previous == "disk_full" and pct_int <= WORKER_DISK_BLOCK_EXIT_PCT:
                self.health_blocked_reason = None
        else:
            low_absolute_free = free_bytes < WORKER_DISK_FREE_FLOOR_BYTES
            if previous is None and pct_int >= WORKER_DISK_BLOCK_ENTER_PCT and low_absolute_free:
                self.health_blocked_reason = "disk_full"
            elif previous == "disk_full" and (pct_int <= WORKER_DISK_BLOCK_EXIT_PCT or not low_absolute_free):
                self.health_blocked_reason = None

        return self.health_blocked_reason != previous


class WorkerRegistry:
    """Tracks build workers. Entries persist until explicitly removed."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._workers: dict[str, Worker] = {}
        self._path: Optional[Path] = Path(path) if path is not None else None
        self._last_saved_at: float = 0.0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Atomically write every worker to ``path``. No-op without a path."""
        if self._path is None:
            return
        payload = {
            "version": _SCHEMA_VERSION,
            "workers": [w.to_persist_dict() for w in self._workers.values()],
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self._path)
            self._last_saved_at = time.monotonic()
        except OSError:
            logger.exception("Failed to persist worker registry to %s", self._path)

    def load(self) -> None:
        """Restore workers from ``path``. Tolerates a missing or corrupt file.

        In-flight job state is not restored — the queue's own restart
        recovery resets WORKING jobs to PENDING, so ``current_job_id``
        starts empty and refills as workers claim.
        """
        if self._path is None:
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            logger.exception("Failed to read worker registry %s; starting empty", self._path)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Worker registry %s is corrupt; starting empty", self._path)
            return
        if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
            logger.error("Worker registry %s has unknown schema; starting empty", self._path)
            return
        entries = data.get("workers")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                worker = Worker.from_persist_dict(entry)
            except Exception:
                logger.error("Skipping malformed worker entry %r", entry.get("client_id"), exc_info=True)
                continue
            self._workers[worker.client_id] = worker
        logger.info("Restored %d worker(s) from %s", len(self._workers), self._path)

    def _persist(self) -> None:
        """Save + broadcast — the tail of every durable mutation."""
        self.save()
        _broadcast_workers_changed()

    def _find_offline_by_hostname(self, hostname: str, threshold_secs: int) -> Optional[Worker]:
        """First offline worker with this hostname, or None.

        Used when a worker registers without a persisted client_id: rather
        than minting a new UUID (and leaving the old row behind forever),
        the fresh registration takes over the offline row. Online rows are
        never adopted — two live workers may legitimately share a hostname.
        """
        for w in self._workers.values():
            if w.hostname == hostname and not self.is_online(w.client_id, threshold_secs):
                return w
        return None

    def register(
        self,
        hostname: str,
        platform: str,
        client_version: Optional[str] = None,
        existing_client_id: Optional[str] = None,
        max_parallel_jobs: int = 1,
        system_info: Optional[dict] = None,
        image_version: Optional[str] = None,
        tags: Optional[list[str]] = None,
        disk_quota_bytes: Optional[int] = None,
        offline_threshold_secs: int = 30,
    ) -> str:
        """Register a worker. Returns client_id.

        If *existing_client_id* is provided, reuse it — even if the server
        doesn't remember the worker (e.g. add-on restart wiped in-memory
        state). This preserves device-registry identity across server
        restarts so HA doesn't end up with duplicate worker devices (#49).

        Without an id, an offline worker with the same hostname is adopted
        (its client_id is handed back) so a wiped volume or an older client
        that discarded its id on shutdown doesn't leave a duplicate row.
        """
        if not existing_client_id:
            adopted = self._find_offline_by_hostname(hostname, offline_threshold_secs)
            if adopted is not None:
                logger.info(
                    "Worker %s registered without an id; adopting offline entry %s",
                    hostname, adopted.client_id,
                )
                existing_client_id = adopted.client_id
        if existing_client_id:
            client_id = existing_client_id
            worker = self._workers.get(client_id)
            if worker is not None:
                worker.hostname = hostname
                worker.platform = platform
                worker.client_version = client_version
                worker.image_version = image_version
                worker.max_parallel_jobs = max_parallel_jobs
                if worker.requested_max_parallel_jobs == max_parallel_jobs:
                    worker.requested_max_parallel_jobs = None
                worker.last_seen = _utcnow()
                worker.stopped = False
                if system_info is not None:
                    worker.system_info = system_info
                if tags is not None:
                    worker.tags = list(tags)
                worker.disk_quota_bytes = disk_quota_bytes
                logger.info(
                    "Re-registered worker %s (%s / %s / v%s / image=%s / %d slots)",
                    client_id, hostname, platform, client_version or "?",
                    image_version or "?", max_parallel_jobs,
                )
            else:
                # #49: server restarted but the client persisted its ID —
                # re-create the Worker with the SAME id rather than minting
                # a fresh UUID. Without this, every add-on restart created
                # a parallel device in HA's registry and the old one would
                # only be removed by the 30-s stale-cleanup pass (if ever).
                worker = Worker(
                    client_id=client_id,
                    hostname=hostname,
                    platform=platform,
                    client_version=client_version,
                    image_version=image_version,
                    max_parallel_jobs=max_parallel_jobs,
                    system_info=system_info,
                    tags=list(tags) if tags is not None else [],
                    disk_quota_bytes=disk_quota_bytes,
                )
                self._workers[client_id] = worker
                logger.info(
                    "Re-attached worker %s (%s / %s / v%s / image=%s / %d slots) "
                    "— server didn't know this client, reusing persisted ID",
                    client_id, hostname, platform, client_version or "?",
                    image_version or "?", max_parallel_jobs,
                )
            self._persist()
            return client_id

        client_id = str(uuid.uuid4())
        worker = Worker(
            client_id=client_id,
            hostname=hostname,
            platform=platform,
            client_version=client_version,
            image_version=image_version,
            max_parallel_jobs=max_parallel_jobs,
            system_info=system_info,
            tags=list(tags) if tags is not None else [],
            disk_quota_bytes=disk_quota_bytes,
        )
        self._workers[client_id] = worker
        logger.info(
            "Registered worker %s (%s / %s / v%s / image=%s / %d slots)",
            client_id, hostname, platform, client_version or "?",
            image_version or "?", max_parallel_jobs,
        )
        self._persist()
        return client_id

    def heartbeat(self, client_id: str, system_info: Optional[dict] = None) -> bool:
        """Update last_seen for *client_id*. Returns False if unknown."""
        worker = self._workers.get(client_id)
        if worker is None:
            return False
        worker.last_seen = _utcnow()
        if worker.stopped:
            worker.stopped = False
            _broadcast_workers_changed()
        if system_info is not None:
            worker.system_info = system_info
            # #219: re-evaluate the disk-pressure self-pause state on every
            # heartbeat so the gate flips within a single heartbeat tick of
            # the disk recovering. Broadcast on transition so the UI repaints
            # without waiting for the 1 Hz SWR poll.
            if worker.evaluate_health():
                logger.info(
                    "Worker %s (%s) health_blocked_reason=%s (disk_used_pct=%s)",
                    client_id, worker.hostname, worker.health_blocked_reason,
                    system_info.get("disk_used_pct"),
                )
                _broadcast_workers_changed()
        if time.monotonic() - self._last_saved_at >= _HEARTBEAT_SAVE_INTERVAL_SECS:
            self.save()
        return True

    def mark_stopped(self, client_id: str) -> bool:
        """Clean worker shutdown: keep the entry, flip it offline now. False if unknown."""
        worker = self._workers.get(client_id)
        if worker is None:
            return False
        worker.stopped = True
        worker.current_job_id = None
        logger.info("Worker %s (%s) stopped (clean shutdown) — kept in registry", client_id, worker.hostname)
        self._persist()
        return True

    def set_job(self, client_id: str, job_id: Optional[str]) -> bool:
        """Set the current job for a worker. Returns False if unknown."""
        worker = self._workers.get(client_id)
        if worker is None:
            return False
        worker.current_job_id = job_id
        return True

    def get_all(self) -> list[Worker]:
        return list(self._workers.values())

    def is_online(self, client_id: str, threshold_secs: int = 30) -> bool:
        worker = self._workers.get(client_id)
        if worker is None or worker.stopped:
            return False
        elapsed = (_utcnow() - worker.last_seen).total_seconds()
        return elapsed <= threshold_secs

    def set_tags(self, client_id: str, tags: list[str]) -> bool:
        """Update a worker's in-memory tags. Returns False if unknown.

        TG.1: callers (the UI tag-edit endpoint, the registration handler)
        also persist via WorkerTagStore so the value survives a restart;
        this in-memory copy keeps /ui/api/workers responses cheap.
        """
        worker = self._workers.get(client_id)
        if worker is None:
            return False
        worker.tags = list(tags)
        self._persist()
        return True

    def set_disk_quota(self, client_id: str, quota_bytes: Optional[int]) -> bool:
        """Update a worker's in-memory disk-quota override. Returns False if unknown.

        DQ.5: callers (the UI quota-edit endpoint, the registration handler)
        also persist via WorkerDiskQuotaStore so the value survives a restart;
        this in-memory copy keeps /ui/api/workers responses cheap and lets the
        next heartbeat pick up the new value without a disk read.
        """
        worker = self._workers.get(client_id)
        if worker is None:
            return False
        worker.disk_quota_bytes = quota_bytes
        self._persist()
        return True

    def set_disabled(self, client_id: str, disabled: bool) -> bool:
        """Enable or disable a worker. Returns False if unknown."""
        worker = self._workers.get(client_id)
        if worker is None:
            return False
        worker.disabled = disabled
        logger.info("Worker %s (%s) %s", client_id, worker.hostname, "disabled" if disabled else "enabled")
        self._persist()
        return True

    def remove(self, client_id: str) -> bool:
        """Remove a worker from the registry. Returns False if unknown."""
        worker = self._workers.pop(client_id, None)
        if worker is None:
            return False
        logger.info("Removed worker %s (%s)", client_id, worker.hostname)
        self._persist()
        return True

    def get(self, client_id: str) -> Optional[Worker]:
        return self._workers.get(client_id)


# Backwards-compatible alias — keeps any code that imports ClientRegistry working
ClientRegistry = WorkerRegistry
