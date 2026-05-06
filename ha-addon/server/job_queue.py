"""Job queue with persistence, state machine, and timeout tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

QUEUE_FILE = Path("/data/queue.json")
MAX_RETRIES = 3
MAX_LOG_BYTES = 512 * 1024  # 512 KB per job
LOG_TRUNCATED_MARKER = "\n\n--- LOG TRUNCATED (exceeded 512 KB) ---\n"


class JobState(str, Enum):
    PENDING = "pending"
    WORKING = "working"
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    # TG.3: a job that no online worker is eligible to claim under the
    # current routing-rule set. Distinct from PENDING so the UI can surface
    # the "stuck because of a rule" state with a different badge + tooltip
    # (the QueueTab `BLOCKED` badge added in TG.9). Transitions:
    #   PENDING → BLOCKED   when re-eval finds zero eligible workers
    #   BLOCKED → PENDING   when re-eval finds at least one eligible worker
    #   BLOCKED → CANCELLED user-initiated (same affordance as PENDING)
    BLOCKED = "blocked"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    return datetime.fromisoformat(s)


@dataclass
class Job:
    id: str
    target: str
    esphome_version: str
    state: JobState
    run_id: str
    assigned_client_id: Optional[str] = None
    assigned_hostname: Optional[str] = None  # persisted so UI works after worker deregisters
    assigned_at: Optional[datetime] = None
    worker_id: Optional[int] = None
    timeout_seconds: int = 600
    created_at: datetime = field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    retry_count: int = 0
    log: Optional[str] = None
    ota_result: Optional[str] = None
    ota_only: bool = False  # skip compile, just re-run OTA upload
    validate_only: bool = False  # run esphome config (validation) instead of compile+OTA
    # FD.1: run `esphome compile` (no OTA) and upload the resulting
    # binary back to the server for download. Mutually exclusive with
    # validate_only.
    download_only: bool = False
    # SOTA.1: compile on worker, OTA from server. Worker runs esphome compile
    # (same as download_only) and uploads the binary; server then runs
    # `esphome upload --device <ota_address> --file <bin> <target.yaml>`.
    # Used for Thread/Matter devices whose IPv6 mesh is only reachable from
    # the HA host. Any worker can compile; OTA is always server-side.
    server_ota: bool = False
    # FD.1: set to True once the worker has POSTed the binary to
    # /api/v1/jobs/{id}/firmware. Drives the Queue-tab Download button.
    has_firmware: bool = False
    ota_address: Optional[str] = None  # override OTA target address (used after rename)
    pinned_client_id: Optional[str] = None  # only this client can claim the job
    # #23: True if this job is a coalesced "follow-up" — created while another
    # job for the same target was already WORKING. Follow-ups are not eligible
    # to be claimed until their predecessor reaches a terminal state. Surfaced
    # in the UI so the user can see "queued behind running" without inferring
    # it from state. At most one follow-up per target at a time; subsequent
    # enqueue calls update the existing follow-up's esphome_version /
    # pinned_client_id rather than creating new entries.
    is_followup: bool = False
    scheduled: bool = False  # True if triggered by the cron scheduler (not a manual action)
    # #92: when scheduled, distinguish recurring (cron) from one-time fires so
    # the Queue tab can show which kind triggered the job. None for user-triggered.
    schedule_kind: Optional[str] = None  # "recurring" | "once" | None
    # Bug 28: True when the enqueue came from Home Assistant's
    # ``esphome_fleet.compile`` / similar service action (identified by
    # the ``esphome_fleet_integration`` system-token Bearer in ha_auth).
    # Lets the Queue tab's Triggered column distinguish HA-driven
    # compiles from user-clicked ones.
    ha_action: bool = False
    # Bug #61: True when the enqueue came from the /ui/api/compile
    # endpoint via the system-token Bearer but NOT from the HA
    # integration (e.g. a direct ``curl`` call, a script, or a
    # third-party tool). Both paths currently carry the same
    # ``esphome_fleet_integration`` ha_user tag because they share the
    # server token; we split them by User-Agent at the endpoint
    # (HomeAssistant/* → ha_action, anything else → api_triggered).
    # Exposed in the Queue's Triggered column with a distinct icon so
    # operators can tell fleet automation from ad-hoc API use.
    api_triggered: bool = False
    # AV.7: HEAD of /config/esphome/ at enqueue time. `None` when the
    # config dir isn't a git repo (hasn't been through AV.1 auto-init)
    # or git_versioning is unavailable. When auto-commit is on, this is
    # the committed state that got compiled. When auto-commit is off,
    # it's whatever the user last committed — the "Diff since last
    # compile" view pairs it with the current working tree to capture
    # both committed AND uncommitted changes since the compile.
    config_hash: Optional[str] = None
    # Bug #8 (1.6.1): human-readable reason this worker was chosen for
    # the job, captured the moment :meth:`claim_next` hands the job off.
    # One of "pinned_to_worker" / "only_online_worker" /
    # "only_eligible_worker" (#99 — rule narrowed the field to one) /
    # "fewer_jobs_than_others" / "higher_perf_score" / "first_available".
    # Surfaced in the Queue + Compile-history tables so an operator can
    # answer "why did this worker pick up this compile" without diffing
    # the scheduler log. ``None`` on jobs from pre-1.6.1 that predated
    # the field.
    selection_reason: Optional[str] = None
    # TG.3: when state == BLOCKED, the rule + summary that disqualified
    # every online worker. Cleared when the job leaves BLOCKED. Shape:
    # ``{"rule_id": str, "rule_name": str, "summary": str}``. The QueueTab
    # tooltip (TG.9) reads ``rule_name`` + ``summary``; ``rule_id`` is
    # used to deep-link the rules-editor open at the offending rule.
    blocked_reason: Optional[dict] = None
    # Bug #97: per-job worker-tag filter set at enqueue time from the
    # Upgrade modal's "Tag expression" worker-selection radio. Same
    # shape as a routing-rule clause: ``{"op": "all_of"|"any_of"|"none_of",
    # "tags": [...]}``. Survives the queue's lifetime (cleared when
    # the job leaves the queue). claim_next honours it via the same
    # eligibility predicate that drives global routing rules. None
    # means "any worker can claim" — the historical default.
    worker_tag_filter: Optional[dict] = None
    # Bug #110: when the user explicitly chooses a Specific worker or
    # Tag expression that conflicts with a global / per-device routing
    # rule, the Upgrade modal surfaces the conflict and lets the user
    # confirm the override. Setting this to True causes the eligibility
    # checks (both the BLOCKED-vs-PENDING re-eval and per-worker
    # claim_next predicate) to ignore routing rules for *this* job —
    # ``pinned_client_id`` and ``worker_tag_filter`` are still honoured
    # because they're the user's explicit constraint, not the rule's.
    # Per-job override; never persisted as a default.
    bypass_routing_rules: bool = False
    status_text: Optional[str] = None  # transient; not persisted
    _streaming_log: str = field(default="", repr=False)  # transient; not persisted

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "target": self.target,
            "esphome_version": self.esphome_version,
            "state": self.state.value,
            "run_id": self.run_id,
            "assigned_client_id": self.assigned_client_id,
            "assigned_hostname": self.assigned_hostname,
            "assigned_at": _iso(self.assigned_at),
            "worker_id": self.worker_id,
            "timeout_seconds": self.timeout_seconds,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "retry_count": self.retry_count,
            "log": self.log,
            "ota_result": self.ota_result,
            "ota_only": self.ota_only,
            "validate_only": self.validate_only,
            "download_only": self.download_only,
            "server_ota": self.server_ota,
            "has_firmware": self.has_firmware,
            "ota_address": self.ota_address,
            "pinned_client_id": self.pinned_client_id,
            "is_followup": self.is_followup,
            "scheduled": self.scheduled,
            "schedule_kind": self.schedule_kind,
            "ha_action": self.ha_action,
            "api_triggered": self.api_triggered,
            "config_hash": self.config_hash,
            "selection_reason": self.selection_reason,
            "blocked_reason": self.blocked_reason,
            "worker_tag_filter": self.worker_tag_filter,
            "bypass_routing_rules": self.bypass_routing_rules,
            "status_text": self.status_text,
            "duration_seconds": self.duration_seconds(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        # Backwards compatibility: old "assigned"/"running" states map to WORKING
        raw_state = d["state"]
        if raw_state in ("assigned", "running"):
            raw_state = "working"
        return cls(
            id=d["id"],
            target=d["target"],
            esphome_version=d["esphome_version"],
            state=JobState(raw_state),
            run_id=d.get("run_id", ""),
            assigned_client_id=d.get("assigned_client_id"),
            assigned_hostname=d.get("assigned_hostname"),
            assigned_at=_from_iso(d.get("assigned_at")),
            worker_id=d.get("worker_id"),
            timeout_seconds=d.get("timeout_seconds", 600),
            created_at=_from_iso(d.get("created_at")) or _utcnow(),
            finished_at=_from_iso(d.get("finished_at")),
            retry_count=d.get("retry_count", 0),
            log=d.get("log"),
            ota_result=d.get("ota_result"),
            ota_only=d.get("ota_only", False),
            validate_only=d.get("validate_only", False),
            download_only=d.get("download_only", False),
            server_ota=d.get("server_ota", False),
            has_firmware=d.get("has_firmware", False),
            ota_address=d.get("ota_address"),
            pinned_client_id=d.get("pinned_client_id"),
            is_followup=d.get("is_followup", False),
            scheduled=d.get("scheduled", False),
            schedule_kind=d.get("schedule_kind"),
            ha_action=d.get("ha_action", False),
            api_triggered=d.get("api_triggered", False),
            config_hash=d.get("config_hash"),
            selection_reason=d.get("selection_reason"),
            blocked_reason=d.get("blocked_reason"),
            worker_tag_filter=d.get("worker_tag_filter"),
            bypass_routing_rules=d.get("bypass_routing_rules", False),
        )

    def duration_seconds(self) -> Optional[float]:
        if self.assigned_at is None:
            return None
        end = self.finished_at or _utcnow()
        return (end - self.assigned_at).total_seconds()


def _purge_firmware(jobs: Iterable["Job"]) -> None:
    """Remove .bin files for removed jobs (FD.7).

    Bug #38: download-only firmware is retained past the job's
    queue lifetime so users can download it long after the queue row
    is cleared; eviction happens under disk-budget pressure rather
    than eager deletion.

    Bug #9 (1.6.1): the worker now archives every successful compile
    (not just download-only), so this protection extends to any job
    whose firmware was actually stored. A job with ``has_firmware``
    set — regardless of ``download_only`` — is preserved until the
    ``firmware_budget_enforcer`` task evicts it or the user deletes
    the history row explicitly. Jobs that failed before producing a
    binary still hit :func:`delete_firmware` defensively to clean up
    any partial writes.

    Imported lazily so unit tests that replace ``DEFAULT_FIRMWARE_DIR``
    via monkeypatch see the patched value.
    """
    try:
        from firmware_storage import delete_firmware  # noqa: PLC0415
    except Exception:
        return
    for job in jobs:
        if getattr(job, "has_firmware", False):
            # Preserve — budget enforcer will evict on disk pressure.
            continue
        try:
            delete_firmware(job.id)
        except Exception:
            logger.debug("Firmware purge for %s raised", job.id, exc_info=True)


class JobQueue:
    """Thread-safe (asyncio) job queue with JSON persistence."""

    def __init__(
        self,
        queue_file: Path = QUEUE_FILE,
        history: "JobHistoryDAO | None" = None,  # type: ignore[name-defined]  # noqa: F821
    ) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = asyncio.Lock()
        self._queue_file = queue_file
        # JH.2: history DAO — snapshots every terminal transition so the
        # /ui/api/history endpoint and per-device drawer can surface
        # past compiles even after the live queue has coalesced them
        # away. Optional; unset in tests that don't exercise history.
        self._history = history

    def _record_history(self, job: Job) -> None:
        """JH.2: snapshot *job* into the persistent history table.

        Swallows any exception — history is best-effort and must never
        prevent a legitimate queue state transition. Logged at DEBUG to
        avoid noise on the fast path.
        """
        if self._history is None:
            return
        try:
            self._history.record_terminal(job)
        except Exception:
            logger.debug(
                "job_history.record_terminal failed for %s (%s); continuing",
                job.id, job.target, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write current queue state to disk. Called after every mutation.

        Also broadcasts a ``queue_changed`` event (#41) so connected HA
        integrations refresh within milliseconds instead of waiting on
        their 30 s polling interval. The broadcast is cheap (no-op when
        no subscribers are connected) and safe to fire on every call —
        piggy-backing on the persist call site means we can't forget it
        at a new mutation point.
        """
        try:
            self._queue_file.parent.mkdir(parents=True, exist_ok=True)
            data = [job.to_dict() for job in self._jobs.values()]
            tmp = self._queue_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._queue_file)
        except Exception:
            logger.exception("Failed to persist queue to %s", self._queue_file)
        try:
            from event_bus import EVENT_QUEUE_CHANGED, broadcast  # noqa: PLC0415
            broadcast(EVENT_QUEUE_CHANGED)
        except Exception:
            logger.debug("event_bus broadcast failed", exc_info=True)

    def load(self) -> None:
        """Load queue from disk on server startup, applying restart recovery rules."""
        if not self._queue_file.exists():
            return
        try:
            data = json.loads(self._queue_file.read_text())
        except Exception:
            logger.exception("Failed to load queue from %s; starting fresh", self._queue_file)
            return

        if not isinstance(data, list):
            logger.error(
                "Queue file %s is not a JSON array (got %s); starting fresh",
                self._queue_file, type(data).__name__,
            )
            return

        skipped = 0
        for d in data:
            if not isinstance(d, dict):
                logger.error("Skipping non-dict entry in queue file: %r", d)
                skipped += 1
                continue
            try:
                job = Job.from_dict(d)
            except Exception:
                # A single bad entry must not take down the whole queue —
                # log the failure at ERROR (so it's visible in production logs)
                # and continue with the rest of the file. B.6 regression guard.
                logger.error(
                    "Failed to parse job entry %r from queue file; skipping",
                    d.get("id", "<no id>"),
                    exc_info=True,
                )
                skipped += 1
                continue
            # Restart recovery: working jobs reset to pending (worker is gone)
            if job.state == JobState.WORKING:
                job.state = JobState.PENDING
                job.assigned_client_id = None
                job.assigned_at = None
            # Bug #18: no longer prune terminal jobs by age on startup.
            # The user clears the queue explicitly from the UI; auto-
            # deleting history on restart meant that a user who scheduled
            # an overnight upgrade and hit a transient problem would come
            # back in the morning to a queue that forgot what happened.
            self._jobs[job.id] = job

        if skipped:
            logger.warning("Skipped %d unparseable job entries on startup", skipped)
        logger.info("Loaded %d jobs from %s (persisted across restarts)", len(self._jobs), self._queue_file)
        # FD.7: sweep orphan firmware binaries whose job no longer
        # exists (add-on crashed mid-cleanup, etc.). Bug #38 / Bug #9:
        # the protected set spans every history row whose binary is
        # still on disk — download-only and OTA alike, since 1.6.1
        # archives the binary for every successful compile.
        # PR #64 review: paginate the history query so a fleet with
        # hundreds of successes doesn't silently lose protection on
        # the older half.
        try:
            from firmware_storage import reconcile_orphans  # noqa: PLC0415
            protected: set[str] = set()
            if self._history is not None:
                try:
                    offset = 0
                    page = 1000
                    while True:
                        rows = self._history.query(state="success", limit=page, offset=offset)
                        if not rows:
                            break
                        for r in rows:
                            if r.get("has_firmware"):
                                protected.add(str(r["id"]))
                        if len(rows) < page:
                            break
                        offset += page
                except Exception:
                    logger.debug(
                        "Couldn't pull protected firmware IDs from history",
                        exc_info=True,
                    )
            reconcile_orphans(
                self._jobs.keys(),
                protected_job_ids=protected,
            )
        except Exception:
            logger.debug("Firmware reconciliation skipped", exc_info=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        target: str,
        esphome_version: str,
        run_id: str,
        timeout_seconds: int,
        validate_only: bool = False,
        download_only: bool = False,
        server_ota: bool = False,
        ota_address: Optional[str] = None,
        pinned_client_id: Optional[str] = None,
        config_hash: Optional[str] = None,
        worker_tag_filter: Optional[dict] = None,
        bypass_routing_rules: bool = False,
    ) -> Optional[Job]:
        """
        Create and enqueue a new job for *target*.

        Coalescing rules (#23) — at most ONE active + ONE follow-up per target:
          - No PENDING/WORKING for target → create new active job (PENDING).
          - PENDING for target (not yet WORKING) → no-op, return None.
            The user's edits will be picked up when the existing job claims
            (the bundle is generated at claim time, not enqueue time).
          - WORKING for target, no follow-up → create a follow-up (PENDING,
            ``is_followup=True``). It will be skipped by ``claim_next`` until
            the WORKING predecessor reaches a terminal state.
          - WORKING for target AND follow-up exists → update the follow-up's
            ``esphome_version``, ``pinned_client_id``, ``ota_address``, and
            ``timeout_seconds`` from the new request, then return it. Lets
            the user "change their mind" about the next compile without
            piling up queue entries.

        Validate-only jobs intentionally bypass coalescing — they're cheap,
        independent, and the user explicitly asked for that specific run.

        Any existing terminal (success/failed/timed_out) jobs for the same
        target are removed so the queue stays tidy.
        """
        async with self._lock:
            # Find current active + follow-up state for this target.
            active: Optional[Job] = None
            followup: Optional[Job] = None
            for job in self._jobs.values():
                if job.target != target:
                    continue
                if job.state == JobState.WORKING:
                    active = job
                elif job.state == JobState.PENDING:
                    if job.is_followup:
                        followup = job
                    else:
                        active = job  # PENDING-but-not-yet-claimed counts as active

            # Validate-only jobs bypass coalescing — see docstring.
            if validate_only:
                pass  # fall through to "create new" path
            elif followup is not None:
                # 1 active + 1 follow-up → update the follow-up in place.
                # Preserves the order in _jobs but reflects the latest user
                # intent (version override, worker pin, etc.).
                followup.esphome_version = esphome_version
                followup.pinned_client_id = pinned_client_id
                followup.ota_address = ota_address
                followup.server_ota = server_ota
                followup.timeout_seconds = timeout_seconds
                followup.run_id = run_id  # belongs to the latest request
                followup.worker_tag_filter = worker_tag_filter
                followup.bypass_routing_rules = bypass_routing_rules
                self._persist()
                logger.info(
                    "Updated existing follow-up job %s for target %s "
                    "(version=%s pinned=%s)",
                    followup.id, target, esphome_version, pinned_client_id,
                )
                return followup
            elif active is not None and active.state == JobState.PENDING:
                # Active is queued but not yet running — no follow-up needed.
                logger.debug(
                    "Target %s already has a pending job %s; skipping enqueue",
                    target, active.id,
                )
                return None
            # else: active is WORKING (or None) → fall through to create.
            # When active is WORKING the new job becomes a follow-up.

            # Clear old terminal jobs (and stale BLOCKED relics — non-terminal
            # but unclaimable, so they'd otherwise sit alongside the new
            # PENDING entry as zombie rows) for this target before adding the
            # new one.
            stale = [
                jid for jid, j in self._jobs.items()
                if j.target == target and j.state in (
                    JobState.SUCCESS, JobState.FAILED, JobState.TIMED_OUT,
                    JobState.CANCELLED, JobState.BLOCKED,
                )
            ]
            # JH.2: defensively record each coalesced job before evicting.
            # Upsert in the DAO makes this idempotent: if the terminal
            # transition path already recorded them, this is a no-op.
            # Guards against a pre-1.6-history queue.json being loaded on
            # a server that *does* want the rows preserved.
            stale_jobs = [self._jobs[jid] for jid in stale]
            for j in stale_jobs:
                self._record_history(j)
            for jid in stale:
                del self._jobs[jid]
            if stale_jobs:
                logger.debug("Removed %d stale job(s) for target %s", len(stale_jobs), target)
                # FD.7: purge any firmware binaries the stale jobs owned
                # so per-target coalescing doesn't leak disk storage.
                # Bug #38: download-only firmware is now retained.
                _purge_firmware(stale_jobs)

            is_followup = active is not None and active.state == JobState.WORKING and not validate_only
            job = Job(
                id=str(uuid.uuid4()),
                target=target,
                esphome_version=esphome_version,
                state=JobState.PENDING,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                validate_only=validate_only,
                download_only=download_only,
                server_ota=server_ota,
                ota_address=ota_address,
                pinned_client_id=pinned_client_id,
                is_followup=is_followup,
                config_hash=config_hash,
                worker_tag_filter=worker_tag_filter,
                bypass_routing_rules=bypass_routing_rules,
            )
            self._jobs[job.id] = job
            self._persist()
            if is_followup:
                logger.info(
                    "Enqueued follow-up job %s for target %s "
                    "(behind running job %s)",
                    job.id, target, active.id if active else "?",
                )
            else:
                logger.info("Enqueued job %s for target %s", job.id, target)
            return job

    async def claim_next(
        self,
        client_id: str,
        worker_id: int = 1,
        hostname: Optional[str] = None,
        faster_idle_worker_exists: bool = False,
        selection_reason_hint: Optional[str] = None,
        is_eligible: Optional[Callable[["Job"], bool]] = None,
    ) -> Optional[Job]:
        """
        Atomically claim the next pending job for *client_id*.

        If *faster_idle_worker_exists* is True, returns None so the
        faster worker can claim on its next poll cycle.

        Bug #8 (1.6.1): *selection_reason_hint* is the upstream
        scheduler's explanation for picking *client_id* — the API
        endpoint computes it from the registry snapshot (fewest jobs,
        higher perf score, only idle worker, etc.). ``claim_next``
        overrides the hint to ``"pinned_to_worker"`` when the job has
        a matching ``pinned_client_id`` — a pinned job's winning
        worker was determined at enqueue time, not at claim time. The
        final reason is persisted on the Job so the Queue + history
        tables can surface it.

        Bug #95 (1.7.0): *is_eligible* is an optional per-worker
        eligibility predicate — caller passes a closure that returns
        True iff this client_id satisfies all routing rules for the
        candidate job. Without it, ``claim_next`` only filters BLOCKED
        jobs, which means a PENDING job (= "at least one fleet worker
        is eligible") could still be claimed by an *ineligible* worker
        — exactly the bug the user reported (RAD GDO devices were
        running on debian + macos workers despite a windows-only
        rule). Pinned jobs bypass the check: pinning is an explicit
        user override, and a re-eval already let the job through to
        PENDING. Mismatched pin + rule is a user-visible conflict, not
        something we silently strand the job for.

        Returns the claimed Job or None if the queue is empty.
        """
        now = _utcnow()
        async with self._lock:
            # #23: a follow-up job is blocked until its predecessor for the
            # same target reaches a terminal state. Pre-compute the set of
            # targets that currently have a WORKING job so we can skip
            # follow-ups for those targets in O(1).
            blocked_targets = {
                j.target for j in self._jobs.values() if j.state == JobState.WORKING
            }
            for job in self._jobs.values():
                # TG.3: PENDING is the only claimable state. BLOCKED jobs
                # are explicitly excluded — the routing-rule re-eval will
                # transition them back to PENDING when an eligible worker
                # comes online.
                if job.state != JobState.PENDING:
                    continue
                # Pinned jobs can only be claimed by the designated worker
                if job.pinned_client_id and job.pinned_client_id != client_id:
                    continue
                # Defer to faster workers — but never defer pinned jobs
                if faster_idle_worker_exists and not job.pinned_client_id:
                    continue
                # Skip follow-ups whose predecessor is still WORKING.
                if job.is_followup and job.target in blocked_targets:
                    continue
                # Bug #95: per-worker routing-rule eligibility check.
                # Pinned jobs bypass — pinning is the user's explicit
                # override; re-eval already cleared the job to PENDING
                # based on fleet-wide eligibility. Without this filter,
                # any worker can claim a PENDING job even when only a
                # subset of the fleet satisfies the required rule.
                if (
                    is_eligible is not None
                    and not job.pinned_client_id
                    and not is_eligible(job)
                ):
                    continue
                job.state = JobState.WORKING
                # Once claimed, a follow-up is no longer "queued behind
                # running" — it IS the running job. Clear the flag so the
                # UI badge disappears at the right moment.
                job.is_followup = False
                job.assigned_client_id = client_id
                job.assigned_hostname = hostname
                job.assigned_at = now
                job.worker_id = worker_id
                # Bug #8: pinning trumps any upstream hint — the user
                # pinned this worker at enqueue time, the scheduler's
                # competitive logic never ran.
                if job.pinned_client_id == client_id:
                    job.selection_reason = "pinned_to_worker"
                elif selection_reason_hint:
                    job.selection_reason = selection_reason_hint
                else:
                    job.selection_reason = "first_available"
                self._persist()
                # #94: include target + hostname so the log line is useful at a
                # glance without correlating IDs back to the registry/queue.
                # Bug #8: log the selection reason too so operators can debug
                # scheduling without digging through the code.
                logger.info(
                    "Job %s (%s) claimed by %s [%s] worker %d — reason=%s",
                    job.id, job.target, hostname or "?", client_id, worker_id,
                    job.selection_reason,
                )
                return job
            return None

    async def submit_result(
        self,
        job_id: str,
        status: str,
        log: Optional[str] = None,
        ota_result: Optional[str] = None,
    ) -> bool:
        """Record the final result of a job.

        Also handles OTA-only updates: if the job is already SUCCESS/FAILED
        and only ota_result is provided (no log), just patch ota_result.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False

            # OTA update on an already-finished job (ota_result required; log is appended if provided)
            if job.state in (JobState.SUCCESS, JobState.FAILED) and ota_result is not None:
                job.ota_result = ota_result
                # Append OTA log from streaming buffer or explicit log
                ota_log = log if log is not None else (job._streaming_log or None)
                if ota_log:
                    job.log = (job.log or "") + "\n" + ota_log
                job._streaming_log = ""
                job.status_text = None
                self._persist()
                # #94: include target + worker hostname in the log line.
                logger.info(
                    "Job %s (%s on %s) OTA result: %s",
                    job_id, job.target, job.assigned_hostname or "?", ota_result,
                )
                # JH.2: upsert history row so the new ota_result lands.
                # The initial compile-result call already wrote state
                # and the null ota_result; this call replaces it.
                self._record_history(job)
                return True

            if job.state != JobState.WORKING:
                # Bug #56: CANCELLED is an expected race here — the user
                # (or smoke suite) cancels a job while a worker's still
                # compiling, the worker runs to completion anyway, and
                # submits its result through this code path. Cancel-
                # during-compile is a documented affordance, not a bug,
                # so log at INFO with a clearer message and leave WARNING
                # for the genuinely-weird cases (duplicate submits, race
                # with retry, etc.) that still deserve operator attention.
                if job.state == JobState.CANCELLED:
                    logger.info(
                        "discarding submit_result for cancelled job %s (%s); "
                        "worker %s finished after cancel",
                        job_id, job.target, job.assigned_client_id or "?",
                    )
                else:
                    logger.warning(
                        "submit_result: job %s (%s) in unexpected state %s",
                        job_id, job.target, job.state,
                    )
                return False
            job.state = JobState.SUCCESS if status == "success" else JobState.FAILED
            # Use the streamed log if the worker didn't send a final log
            job.log = log if log is not None else (job._streaming_log or None)
            job._streaming_log = ""  # free memory
            job.status_text = None
            if ota_result is not None:
                job.ota_result = ota_result
            job.finished_at = _utcnow()
            self._persist()
            # JH.2: snapshot this terminal transition. Inside the async
            # lock so the history row carries the exact in-memory state
            # the persist just wrote. The OTA-patch branch above already
            # returned, so only the real compile result lands here.
            self._record_history(job)
            # #94: include target + worker hostname so log readers can see
            # which device on which worker just finished without joining IDs.
            logger.info(
                "Job %s (%s on %s) finished with status %s",
                job_id, job.target, job.assigned_hostname or "?", status,
            )
            return True

    async def re_evaluate_routing(
        self,
        check_eligibility: "Callable[[Job], tuple[bool, Optional[dict]]]",
    ) -> int:
        """TG.3: sweep PENDING + BLOCKED jobs and adjust state based on the
        caller-supplied eligibility check.

        ``check_eligibility(job)`` returns ``(eligible, blocked_reason)``:
          - ``(True, None)``     → at least one online worker is eligible;
                                    job moves BLOCKED → PENDING (or stays
                                    PENDING).
          - ``(False, reason)``  → no online worker matches the routing
                                    rules for this device; job moves
                                    PENDING → BLOCKED with the supplied
                                    ``reason`` dict on Job.blocked_reason
                                    (or stays BLOCKED, ``reason`` updated
                                    so a renamed rule surfaces correctly).

        Returns the number of jobs whose state changed. Idempotent — a
        defensive watchdog can call this every 30s without churn.
        """
        async with self._lock:
            changed = 0
            for job in self._jobs.values():
                if job.state not in (JobState.PENDING, JobState.BLOCKED):
                    continue
                eligible, reason = check_eligibility(job)
                if eligible:
                    if job.state == JobState.BLOCKED:
                        logger.info(
                            "Job %s (%s): BLOCKED → PENDING (eligible worker available)",
                            job.id, job.target,
                        )
                        job.state = JobState.PENDING
                        job.blocked_reason = None
                        changed += 1
                    elif job.blocked_reason is not None:
                        # Stale reason on a PENDING job — clear it.
                        job.blocked_reason = None
                else:
                    if job.state == JobState.PENDING:
                        logger.info(
                            "Job %s (%s): PENDING → BLOCKED (rule=%s)",
                            job.id, job.target,
                            (reason or {}).get("rule_id", "?"),
                        )
                        job.state = JobState.BLOCKED
                        job.blocked_reason = reason
                        changed += 1
                    elif job.blocked_reason != reason:
                        # Same state, but rule that fires changed (e.g.
                        # rule renamed). Update the reason silently.
                        job.blocked_reason = reason
            if changed:
                self._persist()
            return changed

    async def cancel(self, job_ids: list[str]) -> int:
        """Cancel jobs by id; transitions any non-terminal job to CANCELLED.

        Bug #21: emits an INFO log line per cancelled job so the
        `submit_result: job <uuid> in unexpected state CANCELLED` warning
        that fires when a worker later tries to report on a job the user
        already cancelled is unambiguously explainable in the log.
        """
        async with self._lock:
            cancelled = 0
            just_cancelled: list[Job] = []
            for job_id in job_ids:
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                if job.state in (JobState.PENDING, JobState.WORKING, JobState.BLOCKED):
                    prior_state = job.state
                    job.state = JobState.CANCELLED
                    job.finished_at = _utcnow()
                    job.log = (job.log or "") + "\nCancelled by user."
                    cancelled += 1
                    just_cancelled.append(job)
                    logger.info(
                        "Cancelled job %s (%s) from state %s%s",
                        job_id,
                        job.target,
                        prior_state.value if hasattr(prior_state, "value") else prior_state,
                        f" — worker {job.assigned_hostname or job.assigned_client_id} may still be compiling"
                        if prior_state == JobState.WORKING
                        else "",
                    )
            if cancelled:
                self._persist()
                # JH.2: record each cancellation as a terminal row.
                for job in just_cancelled:
                    self._record_history(job)
            return cancelled

    async def check_timeouts(
        self,
        is_worker_online: "Callable[[str], bool] | None" = None,
    ) -> list[Job]:
        """
        Find timed-out or abandoned jobs (WORKING without a live worker).

        A WORKING job is re-queued (or permanently failed after retries) if
        either:
          - elapsed since ``assigned_at`` ≥ ``timeout_seconds`` — the classic
            "compile got stuck" case; or
          - the assigned worker is no longer online (last heartbeat beyond
            the registry's offline threshold) and *is_worker_online* is
            provided — bug #17. A worker that sleeps / crashes mid-job used
            to leave its jobs stalled for the full ``JOB_TIMEOUT`` (600s by
            default); liveness short-circuit re-queues them as soon as the
            registry notices the worker is gone.

        Re-enqueues as PENDING if retry_count < MAX_RETRIES, otherwise
        marks FAILED permanently.  Returns the list of affected jobs.
        """
        async with self._lock:
            now = _utcnow()
            affected: list[Job] = []
            for job in self._jobs.values():
                if job.state != JobState.WORKING:
                    continue
                if job.assigned_at is None:
                    continue
                elapsed = (now - job.assigned_at).total_seconds()
                timed_out = elapsed >= job.timeout_seconds
                abandoned = (
                    not timed_out
                    and is_worker_online is not None
                    and job.assigned_client_id is not None
                    and not is_worker_online(job.assigned_client_id)
                )
                if not (timed_out or abandoned):
                    continue

                job.retry_count += 1
                if abandoned:
                    logger.warning(
                        "Job %s abandoned after %.0fs — worker %s is offline (retry %d/%d)",
                        job.id,
                        elapsed,
                        job.assigned_client_id,
                        job.retry_count,
                        MAX_RETRIES,
                    )
                else:
                    logger.warning(
                        "Job %s timed out after %.0fs (retry %d/%d)",
                        job.id,
                        elapsed,
                        job.retry_count,
                        MAX_RETRIES,
                    )
                if job.retry_count >= MAX_RETRIES:
                    job.state = JobState.FAILED
                    job.finished_at = now
                    reason = "timeouts" if timed_out else "offline-worker requeues"
                    job.log = (job.log or "") + f"\nPermanently failed after {MAX_RETRIES} {reason}."
                    # JH.2: record the permanent failure. Non-permanent
                    # retries (the else branch) go back to PENDING and
                    # aren't terminal yet — don't record those.
                    # PR #64 review: history write MUST happen after
                    # the queue-side persist, not before. An SIGKILL
                    # between history-write and persist leaves history
                    # with a FAILED row but queue.json still WORKING;
                    # load()'s WORKING → PENDING reset then re-enqueues
                    # the job, producing a second terminal row (maybe
                    # SUCCESS) for the same id. Other terminal sites
                    # (submit_result, cancel) already persist-then-record;
                    # this one was inverted. Defer the record to after
                    # the `if affected: self._persist()` below so the
                    # ordering invariant holds.
                else:
                    # CR.4: dropped the `job.state = JobState.TIMED_OUT`
                    # write that used to sit here — the very next line
                    # overwrote it with PENDING, and no persist/event ran
                    # in between, so the transient TIMED_OUT value was
                    # never observable. The retry path is semantically
                    # "we abandoned this claim; re-enqueue as PENDING".
                    job.state = JobState.PENDING
                    job.assigned_client_id = None
                    job.assigned_at = None

                affected.append(job)

            if affected:
                self._persist()
                # Record history for permanently-failed rows *after*
                # the persist has landed (see PR #64 comment inside
                # the loop above). Non-terminal (PENDING-requeued)
                # rows are filtered out by state.
                for j in affected:
                    if j.state == JobState.FAILED:
                        self._record_history(j)
            return affected

    async def retry(
        self,
        job_ids: list[str],
        esphome_version: str,
        run_id: str,
        timeout_seconds: int,
        target_versions: dict[str, str] | None = None,
        config_hash: Optional[str] = None,
    ) -> list["Job"]:
        """Re-enqueue failed/timed_out/cancelled/success jobs as new PENDING jobs.

        The old job being retried is removed; any other terminal jobs for the
        same target are also cleared (same semantics as enqueue).

        *target_versions* maps target filenames to per-device ESPHome versions
        (#51). When a device is pinned to a specific version, the retry should
        use that version instead of the *esphome_version* default.
        """
        async with self._lock:
            new_jobs: list[Job] = []
            for job_id in job_ids:
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                is_failed = job.state in (JobState.FAILED, JobState.TIMED_OUT, JobState.CANCELLED)
                is_ota_failed = job.state == JobState.SUCCESS and job.ota_result == "failed"
                is_success = job.state == JobState.SUCCESS
                if not (is_failed or is_ota_failed or is_success):
                    continue
                target = job.target
                # Pin OTA retries to the worker that compiled the firmware.
                # Also preserve any user-requested pin from "Upgrade on..." action.
                pin_to = job.assigned_client_id if is_ota_failed else job.pinned_client_id
                # Remove all terminal jobs for this target (including the one being retried)
                stale = [
                    jid for jid, j in self._jobs.items()
                    if j.target == target and j.state in (
                        JobState.SUCCESS, JobState.FAILED, JobState.TIMED_OUT, JobState.CANCELLED
                    )
                ]
                # JH.2: defensively snapshot retried-away jobs. Idempotent
                # via upsert — if they were already recorded during their
                # terminal transition, nothing changes.
                for jid in stale:
                    self._record_history(self._jobs[jid])
                for jid in stale:
                    del self._jobs[jid]
                version_for_target = (target_versions or {}).get(target, esphome_version)
                new_job = Job(
                    id=str(uuid.uuid4()),
                    target=target,
                    esphome_version=version_for_target,
                    state=JobState.PENDING,
                    run_id=run_id,
                    timeout_seconds=timeout_seconds,
                    ota_only=is_ota_failed,
                    server_ota=job.server_ota,
                    pinned_client_id=pin_to,
                    config_hash=config_hash,
                )
                self._jobs[new_job.id] = new_job
                new_jobs.append(new_job)
                logger.info(
                    "Retrying → new job %s for %s (ota_only=%s, pinned=%s)",
                    new_job.id, target, is_ota_failed, pin_to or "any",
                )
            if new_jobs:
                self._persist()
            return new_jobs

    async def patch_ota_result(
        self, job_id: str, ota_result: str, log: Optional[str] = None
    ) -> None:
        """SOTA.2: set ota_result on an already-terminal job after server-side OTA.

        Does not re-run the state machine — job remains SUCCESS. Appends the
        server OTA log to any existing log so the Queue tab log modal shows it.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.ota_result = ota_result
            if log:
                job.log = (job.log or "") + "\n--- Server OTA ---\n" + log
            self._persist()

    async def update_status(self, job_id: str, status_text: str) -> bool:
        """Update the in-progress status text for a running job (not persisted)."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status_text = status_text
            return True

    async def append_log(self, job_id: str, text: str) -> bool:
        """Append streaming log text to a running job (transient; not persisted).

        Caps the streaming log at MAX_LOG_BYTES to prevent OOM from
        runaway build output.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            current_len = len(job._streaming_log)
            if current_len >= MAX_LOG_BYTES:
                return True  # silently drop — already truncated
            # Reserve space for the truncation marker so the final log
            # never exceeds MAX_LOG_BYTES, and never concatenate the full
            # incoming text first (which would itself risk OOM).
            budget = MAX_LOG_BYTES - current_len
            if len(text) <= budget:
                job._streaming_log += text
            else:
                # Truncating. Final log must not exceed MAX_LOG_BYTES,
                # including the marker — trim the existing log if needed.
                marker_len = len(LOG_TRUNCATED_MARKER)
                if budget >= marker_len:
                    job._streaming_log += text[: budget - marker_len] + LOG_TRUNCATED_MARKER
                else:
                    trim_to = max(0, MAX_LOG_BYTES - marker_len)
                    job._streaming_log = job._streaming_log[:trim_to] + LOG_TRUNCATED_MARKER
            return True

    def get_all(self) -> list[Job]:
        """Return a snapshot of all jobs."""
        return list(self._jobs.values())

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def queue_size(self) -> int:
        """Return number of pending/working jobs."""
        return sum(
            1
            for j in self._jobs.values()
            if j.state in (JobState.PENDING, JobState.WORKING)
        )

    async def prune_old_terminal(self, max_age_seconds: int = 3600) -> int:
        """Remove terminal jobs older than *max_age_seconds*. Returns count removed."""
        terminal = {JobState.SUCCESS, JobState.FAILED, JobState.TIMED_OUT, JobState.CANCELLED}
        cutoff = datetime.now(timezone.utc)
        async with self._lock:
            to_remove = []
            for job_id, job in self._jobs.items():
                if job.state not in terminal:
                    continue
                try:
                    created = job.created_at if isinstance(job.created_at, datetime) else datetime.fromisoformat(str(job.created_at))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    age = (cutoff - created).total_seconds()
                    if age > max_age_seconds:
                        to_remove.append(job_id)
                except Exception:
                    to_remove.append(job_id)  # can't parse date → prune it
            for job_id in to_remove:
                del self._jobs[job_id]
            if to_remove:
                self._persist()
            return len(to_remove)

    async def remove_jobs(self, job_ids: list[str]) -> int:
        """Remove terminal jobs by ID. Returns count removed.

        FD.7: deletes the associated firmware binary (if any) alongside
        the queue entry so storage tracks the queue.
        """
        terminal = {JobState.SUCCESS, JobState.FAILED, JobState.TIMED_OUT, JobState.CANCELLED}
        async with self._lock:
            removed_jobs: list[Job] = []
            for job_id in job_ids:
                job = self._jobs.get(job_id)
                if job and job.state in terminal:
                    del self._jobs[job_id]
                    removed_jobs.append(job)
            if removed_jobs:
                self._persist()
        # Bug #38: _purge_firmware now takes Jobs and preserves download_only firmware.
        _purge_firmware(removed_jobs)
        return len(removed_jobs)

    async def clear(self, states: list[str], require_ota_success: bool = False) -> int:
        """Remove terminal jobs whose state is in *states*. Returns count removed.

        If *require_ota_success* is True, jobs with ota_result == 'failed' are
        kept even if their state matches (so "Clear Succeeded" leaves OTA-failed jobs).

        FD.7: firmware binaries for removed jobs are deleted from disk.
        """
        terminal = {JobState.SUCCESS, JobState.FAILED, JobState.TIMED_OUT, JobState.CANCELLED}
        target_states = {JobState(s) for s in states if JobState(s) in terminal}
        async with self._lock:
            to_remove: list[Job] = []
            for job_id, job in self._jobs.items():
                if job.state not in target_states:
                    continue
                if require_ota_success and job.ota_result == "failed":
                    continue
                to_remove.append(job)
            for job in to_remove:
                del self._jobs[job.id]
            if to_remove:
                self._persist()
        # Bug #38: _purge_firmware filters out download_only — those binaries
        # survive user Clear and only evict via the firmware budget task.
        _purge_firmware(to_remove)
        return len(to_remove)

    async def mark_firmware_stored(self, job_id: str) -> bool:
        """Record that a worker has uploaded the .bin for *job_id*.

        Returns True when the flag was flipped. Called by the worker
        firmware-upload endpoint (FD.5).

        Bug #9 (1.6.1): accepts uploads for OTA jobs as well as
        download-only — the worker now archives every successful compile
        on the server. The WORKING-state check remains the real gate; a
        stale worker uploading after requeue or coalesce can't race the
        flag because the API handler already re-runs state + identity
        checks before reaching this method.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.state != JobState.WORKING:
                logger.warning(
                    "Refusing firmware upload for job %s in state %s (not WORKING)",
                    job_id, job.state.value,
                )
                return False
            job.has_firmware = True
            self._persist()
            return True

    def active_job_ids(self) -> set[str]:
        """Snapshot of current job ids — used by firmware-storage reconciliation."""
        return set(self._jobs.keys())
