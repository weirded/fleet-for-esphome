"""REST API handlers for build workers (/api/v1/*)."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import tarfile
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
from pydantic import ValidationError

from app_config import AppConfig
from constants import (
    HEADER_X_CLIENT_ID, HEADER_X_WORKER_ID,
    MIN_IMAGE_VERSION,
    SERVER_OTA_ABSOLUTE_TIMEOUT, SERVER_OTA_IDLE_TIMEOUT,
)
from job_queue import Job, JobState
from protocol import (
    PROTOCOL_VERSION,
    DeregisterRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    JobAssignment,
    JobLogAppend,
    JobResultSubmission,
    JobStatusUpdate,
    OkResponse,
    ProtocolError,
    RegisterRequest,
    RegisterResponse,
    WorkerDiagnosticsUpload,
    WorkerLogAppend,
)
import firmware_storage
import scanner as _scanner
from scanner import create_bundle_async, get_esphome_version

# Worker code bundled inside this container
_CLIENT_CODE_DIR = Path("/app/client")
_VERSION_FILE = Path("/app/VERSION")


@lru_cache(maxsize=1)
def _get_server_client_version() -> str:
    """Return the add-on version from /app/VERSION (set at image build time)."""
    try:
        return _VERSION_FILE.read_text().strip()
    except Exception:
        return "0.0.1"

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _cfg(request: web.Request) -> AppConfig:
    return request.app["config"]


def _unauthorized() -> web.Response:
    """Return a 401 response.

    Kept as a thin helper because some downstream tests still call it; the
    in-handler ``_check_auth`` was removed in C.7 — every ``/api/v1/*``
    request is gated by ``main.auth_middleware`` before it reaches a handler,
    and a duplicated check in the handler can drift (e.g. the middleware
    uses ``constant_time_compare``; a future fix applied to one site
    wouldn't reach the other).
    """
    return web.json_response({"error": "Unauthorized"}, status=401)


def _protocol_error(error: str, reason: Optional[str] = None, status: int = 400) -> web.Response:
    """Return a structured ProtocolError response with HTTP *status*."""
    body = ProtocolError(error=error, reason=reason).model_dump(exclude_none=True)
    return web.json_response(body, status=status)


def _first_error_reason(exc: ValidationError) -> str:
    """Format the first pydantic validation error as a short human string."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {first.get('msg', 'invalid')}"


async def _parse_body(request: web.Request, model_cls):  # type: ignore[no-untyped-def]
    """Parse + validate a JSON body into *model_cls*.

    Returns ``(model, None)`` on success or ``(None, response)`` on failure,
    so callers can ``model, err = await _parse_body(...)`` and return ``err``.
    """
    try:
        body = await request.json()
    except Exception:
        return None, _protocol_error("invalid_json")
    if not isinstance(body, dict):
        return None, _protocol_error("invalid_payload", "expected JSON object")
    try:
        return model_cls.model_validate(body), None
    except ValidationError as exc:
        return None, _protocol_error("invalid_payload", _first_error_reason(exc))


def _check_protocol_version(msg) -> Optional[web.Response]:  # type: ignore[no-untyped-def]
    """Reject messages whose ``protocol_version`` is unknown to this server.

    Today we only know version 1; accept that and reject everything else
    with a structured error so the worker can log it clearly instead of
    acting on a half-processed payload.
    """
    pv = getattr(msg, "protocol_version", None)
    if pv is None:
        return None
    if pv != PROTOCOL_VERSION:
        return _protocol_error(
            "unsupported_protocol_version",
            f"server speaks v{PROTOCOL_VERSION}, received v{pv}",
        )
    return None


async def _register_worker_handler(request: web.Request) -> web.Response:
    msg, err = await _parse_body(request, RegisterRequest)
    if err is not None:
        return err
    assert msg is not None
    pv_err = _check_protocol_version(msg)
    if pv_err is not None:
        return pv_err

    from helpers import clamp  # noqa: PLC0415
    max_parallel_jobs = clamp(msg.max_parallel_jobs, 0, 32)
    system_info_dict = msg.system_info.model_dump(exclude_none=True) if msg.system_info else None

    # TG.1: resolve tags through the persistent store. Identity is the worker's
    # hostname (the spec's primary key), falling back to its persistent client_id
    # when two workers happen to share a hostname. The first registration for an
    # identity seeds from the worker's WORKER_TAGS env; subsequent ones are
    # server-side-wins unless the worker also set WORKER_TAGS_OVERWRITE=1.
    identity = msg.hostname or msg.client_id or ""
    tag_store = request.app.get("worker_tag_store")
    if tag_store is not None:
        resolved_tags: Optional[list[str]] = tag_store.load_or_seed(
            identity, msg.tags, overwrite=msg.overwrite_tags,
        )
    else:
        # Test rigs that don't construct a tag store still get the in-memory
        # echo of whatever the worker sent — preserves prior test contracts
        # for code paths that don't care about persistence.
        resolved_tags = list(msg.tags) if msg.tags is not None else None

    # DQ.4: resolve the per-worker disk-quota override the same way as tags.
    # First registration seeds from RegisterRequest.disk_quota_bytes (the value
    # baked into ``-e WORKER_DISK_QUOTA_GB=N`` on the docker run command).
    # Later registrations are always server-side-wins (no overwrite flag —
    # there's no scripted-multi-worker use case here yet).
    quota_store = request.app.get("worker_disk_quota_store")
    if quota_store is not None:
        resolved_quota: Optional[int] = quota_store.load_or_seed(
            identity, msg.disk_quota_bytes,
        )
    else:
        resolved_quota = msg.disk_quota_bytes

    registry = request.app["registry"]
    # Adoption of an offline same-hostname row (registry.register) uses the
    # same offline threshold the UI does, so "offline" means the same thing
    # in both places.
    from settings import get_settings as _get_settings  # noqa: PLC0415
    client_id = registry.register(
        msg.hostname,
        msg.platform,
        msg.client_version,
        msg.client_id,
        max_parallel_jobs,
        system_info_dict,
        image_version=msg.image_version,
        tags=resolved_tags,
        disk_quota_bytes=resolved_quota,
        offline_threshold_secs=_get_settings().worker_offline_threshold,
    )
    # TG.3: a new worker (or one re-registering with different tags)
    # may unblock or newly-block PENDING jobs. Fire-and-forget — the
    # registration response shouldn't wait on the sweep.
    from routing_eligibility import fire_and_forget  # noqa: PLC0415
    fire_and_forget(request.app)
    return web.json_response(RegisterResponse(client_id=client_id).model_dump(exclude_none=True))


async def _heartbeat_handler(request: web.Request) -> web.Response:
    msg, err = await _parse_body(request, HeartbeatRequest)
    if err is not None:
        return err
    assert msg is not None
    pv_err = _check_protocol_version(msg)
    if pv_err is not None:
        return pv_err

    system_info_dict = msg.system_info.model_dump(exclude_none=True) if msg.system_info else None
    registry = request.app["registry"]
    if not registry.heartbeat(msg.client_id, system_info_dict):
        # Unknown worker — let it re-register
        return _protocol_error("unknown_client_id", status=404)

    # Build response from registry state
    worker = registry.get(msg.client_id)
    resp = HeartbeatResponse(ok=True)

    # Only advertise a newer source-code version to workers running an
    # up-to-date Docker image. A stale image can't be fixed by rewriting
    # .py files in place (missing system deps / Python version / libs),
    # so suppressing server_client_version prevents an auto-update loop
    # that would just fail to pick up the real changes.
    if worker is None or _image_version_ok(worker.image_version):
        resp.server_client_version = _get_server_client_version()
    else:
        resp.image_upgrade_required = True
        resp.min_image_version = MIN_IMAGE_VERSION

    if worker and worker.requested_max_parallel_jobs is not None:
        resp.set_max_parallel_jobs = worker.requested_max_parallel_jobs
    if worker and worker.pending_clean:
        resp.clean_build_cache = True
        worker.pending_clean = False

    # DQ.4: push the effective disk quota on every heartbeat so a UI edit
    # propagates within one tick (≤10s by default) without restarting the
    # worker. Override (Worker.disk_quota_bytes) wins over the fleet default
    # (AppSettings.default_worker_disk_quota_bytes); we always send a value
    # so the worker's local cell stays in sync — never None on the wire.
    if worker is not None:
        from settings import get_settings as _gs  # noqa: PLC0415
        default_quota = _gs().default_worker_disk_quota_bytes
        resp.set_disk_quota_bytes = worker.effective_disk_quota_bytes(default_quota)

    # WL.2: tell the worker to start (or stop) streaming logs based on
    # whether any UI is currently watching. Explicit True/False — never
    # leave it as None once the feature is live because the worker uses
    # the explicit False to tear its pusher thread down.
    broker = request.app.get("worker_log_broker")
    if broker is not None:
        resp.stream_logs = broker.is_watched(msg.client_id)

    # #109: if the UI has asked for a thread dump from this worker,
    # surface the pending request id. The worker will POST the dump to
    # /api/v1/workers/{id}/diagnostics and we'll clear the slot once
    # it arrives (see `receive_worker_diagnostics`).
    diag = request.app.get("diagnostics_broker")
    if diag is not None:
        pending = diag.pending_for_worker(msg.client_id)
        if pending is not None:
            resp.diagnostics_request_id = pending

    return web.json_response(resp.model_dump(exclude_none=True))


def _image_version_ok(reported: Optional[str]) -> bool:
    """Return True if *reported* is >= MIN_IMAGE_VERSION.

    Image versions are monotonic integers (as strings). A missing/invalid
    reported version is treated as out of date — workers that never send
    an image_version field are pre-LIB.0 builds and should be upgraded.
    """
    if reported is None:
        return False
    try:
        return int(reported) >= int(MIN_IMAGE_VERSION)
    except (TypeError, ValueError):
        return False


async def _deregister_handler(request: web.Request) -> web.Response:
    """Clean worker shutdown: mark it offline immediately but keep the row.

    Workers only leave the registry when the operator deletes them from
    the Workers tab — a shutdown is not a deletion.
    """
    msg, err = await _parse_body(request, DeregisterRequest)
    if err is not None:
        return err
    assert msg is not None

    registry = request.app["registry"]
    worker = registry.get(msg.client_id)
    hostname = worker.hostname if worker else "?"
    if registry.mark_stopped(msg.client_id):
        logger.info("Worker %s [%s] deregistered (clean shutdown) — kept in registry", hostname, msg.client_id)
        # TG.3: a worker leaving the pool may push PENDING jobs to BLOCKED.
        from routing_eligibility import fire_and_forget  # noqa: PLC0415
        fire_and_forget(request.app)
        return web.json_response(OkResponse().model_dump())
    return _protocol_error("unknown_client_id", status=404)


# ---------------------------------------------------------------------------
# New worker routes (preferred)
# ---------------------------------------------------------------------------

@routes.post("/api/v1/workers/register")
async def register_worker(request: web.Request) -> web.Response:
    return await _register_worker_handler(request)


@routes.post("/api/v1/workers/heartbeat")
async def worker_heartbeat(request: web.Request) -> web.Response:
    return await _heartbeat_handler(request)


@routes.post("/api/v1/workers/deregister")
async def deregister_worker(request: web.Request) -> web.Response:
    return await _deregister_handler(request)


# ---------------------------------------------------------------------------
# Legacy client routes — kept for backwards compatibility with deployed workers
# that haven't updated yet. These call the same handlers.
# ---------------------------------------------------------------------------

@routes.post("/api/v1/clients/register")
async def register_client(request: web.Request) -> web.Response:
    return await _register_worker_handler(request)


@routes.post("/api/v1/clients/heartbeat")
async def client_heartbeat(request: web.Request) -> web.Response:
    return await _heartbeat_handler(request)


@routes.post("/api/v1/clients/deregister")
async def deregister_client(request: web.Request) -> web.Response:
    return await _deregister_handler(request)


# ---------------------------------------------------------------------------
# Job routes
# ---------------------------------------------------------------------------

@routes.get("/api/v1/jobs/next")
async def get_next_job(request: web.Request) -> web.Response:
    client_id = request.headers.get(HEADER_X_CLIENT_ID) or request.rel_url.query.get("client_id")
    if not client_id:
        return web.json_response({"error": f"{HEADER_X_CLIENT_ID} header or client_id param required"}, status=400)

    queue = request.app["queue"]
    registry = request.app["registry"]
    cfg = _cfg(request)

    # Don't assign new jobs to disabled workers
    worker = registry.get(client_id)
    if worker and worker.disabled:
        return web.Response(status=204)
    # #219: same gate for the self-imposed disk-pressure pause. The
    # registry stamps ``health_blocked_reason`` from heartbeat; once
    # the worker's disk drops back below the exit threshold this clears
    # automatically and the worker resumes claiming on the next poll.
    if worker and worker.health_blocked_reason:
        return web.Response(status=204)

    worker_id_str = request.headers.get(HEADER_X_WORKER_ID, "1")
    try:
        worker_id = int(worker_id_str)
    except ValueError:
        worker_id = 1

    hostname = worker.hostname if worker else None

    # Performance-based scheduling: spread jobs across workers, best-available first.
    # Effective score = perf_score * (1 - cpu_usage/100) — factors in both speed and load.
    # Two rules:
    # 1. Defer if ANY online worker has fewer active jobs (spread evenly first)
    # 2. Among workers with equal job counts, defer if one with higher effective score has free slots
    #
    # Bug #8 (1.6.1): we also record WHY this worker won when it
    # didn't defer — the reason is passed to ``claim_next`` and
    # persisted on the Job so the UI can explain the scheduling
    # decision without requiring an operator to cross-reference
    # the scheduler log.
    # Bug #95: build a per-worker routing-rule eligibility predicate
    # so PENDING jobs can be filtered by *this* worker's tags. Without
    # it, claim_next only filters out BLOCKED jobs — meaning any
    # online worker could grab a PENDING job even when only a subset
    # of the fleet satisfies the required rule.
    from routing_eligibility import build_claim_eligibility  # noqa: PLC0415
    worker_tags_list = list(worker.tags or []) if worker else []
    is_eligible_for = build_claim_eligibility(request.app, worker_tags_list)

    # Bug #95-followup: the scheduler must also know about routing
    # eligibility when deciding whether to defer to a "faster" worker.
    # The PENDING jobs *we* are eligible for, ignoring pinning, drive
    # the deferral check below — if no other worker is eligible for
    # any of them, deferring would just strand the job (the original
    # symptom: ratgdo job stayed PENDING because OPTIPLEX-7 deferred
    # to higher-score macos workers that the windows-only rule
    # disqualified).
    my_eligible_pending = [
        j for j in queue.get_all()
        if j.state == JobState.PENDING
        and not j.is_followup
        and (not j.pinned_client_id or j.pinned_client_id == client_id)
        and is_eligible_for(j)
    ]

    should_defer = False
    # Count other candidate workers so we can pick the most informative
    # reason when we don't defer.
    other_candidate_count = 0
    # Bug #99: count *eligible* other candidates — workers that are
    # eligible-by-tag for at least one of the same PENDING jobs we are.
    # Bug #210: this count must be busy-independent so we don't
    # misattribute "all other workers were fully booked" as
    # ``only_eligible_worker``. ``eligible_other_count_any`` tracks
    # tag-eligibility regardless of free slots; ``eligible_other_free_count``
    # adds the busy filter and drives the deferral logic below.
    eligible_other_count_any = 0
    eligible_other_free_count = 0
    defer_beats_me_on_jobs = False
    defer_beats_me_on_perf = False
    if worker:
        try:
            my_info = worker.system_info or {}
            my_perf = my_info.get("perf_score", 0) or 0  # #234: None guard
            my_cpu = my_info.get("cpu_usage")
            my_effective = my_perf * (1 - (my_cpu or 0) / 100)
            from settings import get_settings  # noqa: PLC0415
            cfg_threshold = get_settings().worker_offline_threshold
            # Count active jobs per worker from the queue
            active_jobs_by_worker: dict[str, int] = {}
            for j in queue.get_all():
                if j.state == JobState.WORKING and j.assigned_client_id:
                    active_jobs_by_worker[j.assigned_client_id] = \
                        active_jobs_by_worker.get(j.assigned_client_id, 0) + 1
            my_active = active_jobs_by_worker.get(client_id, 0)
            # #235: accumulate free slots across all workers that beat us, then
            # cap deferral so we claim overflow when the queue exceeds the
            # higher-priority pool's capacity.
            deferral_pool_free_slots = 0
            for other in registry.get_all():
                if other.client_id == client_id:
                    continue
                if other.disabled or not registry.is_online(other.client_id, cfg_threshold):
                    continue
                # #219: a disk-blocked worker won't claim, so deferring to it
                # would just strand the job. Skip it from the candidate pool
                # the same way an offline/disabled worker is skipped.
                if other.health_blocked_reason:
                    continue
                other_candidate_count += 1
                other_active = active_jobs_by_worker.get(other.client_id, 0)
                other_info = other.system_info or {}
                other_perf = other_info.get("perf_score", 0) or 0  # #234: None guard
                other_cpu = other_info.get("cpu_usage")
                other_effective = other_perf * (1 - (other_cpu or 0) / 100)
                other_free = other_active < other.max_parallel_jobs
                # Bug #98 / #99 / #210: tag-eligibility is computed BEFORE
                # the busy-skip so the reason hint can distinguish "rules
                # narrowed the field" from "others are eligible but busy".
                # Bug #98 (deferral loop) still gates on free-AND-eligible.
                if my_eligible_pending:
                    other_check = build_claim_eligibility(request.app, list(other.tags or []))
                    is_other_eligible = any(
                        other_check(j)
                        and (not j.pinned_client_id or j.pinned_client_id == other.client_id)
                        for j in my_eligible_pending
                    )
                else:
                    is_other_eligible = True
                if is_other_eligible:
                    eligible_other_count_any += 1
                if not other_free:
                    continue  # fully busy, ignore for deferral logic below
                if not is_other_eligible:
                    continue  # eligible-but-ineligible-for-our-jobs — skip deferral
                eligible_other_free_count += 1
                # Rule 1: another worker has fewer jobs — let them catch up
                # Rule 2: same job count but higher effective score — let them go first
                # #235: don't break early; accumulate free slots for the cap below.
                beats_on_jobs = other_active < my_active
                beats_on_perf = other_active == my_active and other_effective > my_effective
                if beats_on_jobs or beats_on_perf:
                    deferral_pool_free_slots += other.max_parallel_jobs - other_active
                else:
                    # This `other` worker LOST to *client_id* — record which
                    # rule did the winning so we can name the reason below.
                    if other_active > my_active:
                        defer_beats_me_on_jobs = True
                    elif other_effective < my_effective:
                        defer_beats_me_on_perf = True
            # #235: only defer when the higher-priority pool has enough free
            # slots to absorb the entire eligible queue; otherwise claim the
            # overflow ourselves instead of letting it sit idle.
            should_defer = (
                deferral_pool_free_slots > 0
                and len(my_eligible_pending) <= deferral_pool_free_slots
            )
        except Exception:
            logger.warning(
                "Scheduler eligibility check failed for client_id=%s;"
                " treating as no-job-for-you (HTTP 204)",
                client_id,
                exc_info=True,
            )
            return web.Response(status=204)

    # Bug #8: compose the reason hint now that we know we aren't
    # deferring. Only evaluated when claim_next actually hands out a
    # job (returns None if nothing matches).
    if other_candidate_count == 0:
        selection_reason_hint = "only_online_worker"
    elif eligible_other_count_any == 0 and my_eligible_pending:
        # Bug #99: other workers are online, but the routing rules
        # disqualified all of them for the jobs in our queue. The
        # reason isn't "first to poll" — it's "rule narrowed to me".
        # Bug #210: count is busy-independent so a fleet where every
        # other worker is eligible-but-fully-booked falls through to
        # ``first_available`` rather than misreporting as ``only_eligible``.
        selection_reason_hint = "only_eligible_worker"
    elif defer_beats_me_on_jobs:
        selection_reason_hint = "fewer_jobs_than_others"
    elif defer_beats_me_on_perf:
        selection_reason_hint = "higher_perf_score"
    else:
        selection_reason_hint = "first_available"

    job = await queue.claim_next(client_id, worker_id, hostname=hostname,
                                  faster_idle_worker_exists=should_defer,
                                  selection_reason_hint=selection_reason_hint,
                                  is_eligible=is_eligible_for)
    if job is None:
        return web.Response(status=204)

    # Generate bundle on demand. BD — ships only the target's
    # referenced files (no `.git/`, no cross-device secrets) via
    # ESPHome's ConfigBundleCreator. Runs off the event loop because
    # bundling re-parses YAML and runs the full validator — same
    # rationale as reseed_device_poller_from_config in main.py.
    # Bug #111: scanner.create_bundle_async serialises concurrent
    # bundle subprocesses around an asyncio.Lock so ESPHome's
    # git.clone_or_update never sees two writers in the same
    # `.esphome/{packages,external_components}/<sha8>/` directory.
    try:
        bundle_bytes = await create_bundle_async(cfg.config_dir, job.target)
        bundle_b64 = base64.b64encode(bundle_bytes).decode("ascii")
    except Exception as exc:
        # BD.1: bundle creation wraps the full ESPHome validator; most
        # failures here are real YAML-schema problems the user needs to
        # fix (e.g. "Duplicate entity", "Only one binary sensor of
        # type 'motion' allowed"). Surface the error on the job itself
        # — status FAILED + exception message as the log — so the
        # Queue-tab row shows the validation message directly. Cancel
        # would leave the user staring at a greyed-out row with no
        # explanation.
        logger.exception("Failed to create bundle for job %s", job.id)

        # #247: not every failure here is the user's YAML. Bundling imports
        # the server's own lazy-installed ESPHome, so a compile dispatched
        # during the 1-3 minute install window dies with
        # `ModuleNotFoundError: No module named 'esphome'` — and then got
        # told to go fix a config that was never wrong. That window opens
        # every time the pinned ESPHome version changes, which the UI makes
        # one click away, and every time upstream publishes a release; it
        # hit the release smoke twice in the 1.7.3 -> 1.8 cycle.
        #
        # The server already knows the install is in flight, so don't just
        # reword the message — put the job back and let the next poll pick
        # it up, which is what a human would do. release_for_retry is
        # bounded by MAX_RETRIES so a genuinely stuck install still lands on
        # a real failure rather than spinning.
        installing = _scanner.esphome_install_in_flight()
        if _scanner.esphome_import_error(exc) or installing:
            released = await queue.release_for_retry(
                job.id, "server ESPHome still installing (#247)"
            )
            if released:
                # 204 = "nothing for you right now". The worker polls again
                # on its normal interval and the job is PENDING again.
                return web.Response(status=204)
            err_msg = (
                f"Bundle creation failed for {job.target}: "
                f"{type(exc).__name__}: {exc}\n\n"
                "The server's ESPHome install did not become ready after "
                "several retries. This is a server-side problem, not a "
                "problem with this config — check the add-on log for "
                "ESPHome install errors."
            )
            await queue.submit_result(job.id, "failed", log=err_msg)
            return web.json_response({"error": "Bundle creation failed"}, status=500)

        err_msg = (
            f"Bundle creation failed for {job.target}: "
            f"{type(exc).__name__}: {exc}\n\n"
            "Fleet for ESPHome validates the target config before dispatching "
            "it to a worker (BD). Fix the YAML error above and re-queue."
        )
        await queue.submit_result(job.id, "failed", log=err_msg)
        return web.json_response({"error": "Bundle creation failed"}, status=500)

    registry.set_job(client_id, job.id)

    # Include server timezone so worker can match ESPHome's timezone detection.
    # Different timezones produce different config_hash → unnecessary clean rebuilds.
    import time as _time  # noqa: PLC0415
    server_tz = _time.tzname[0] if _time.daylight == 0 else _time.tzname[1]
    import os as _os  # noqa: PLC0415

    # Prefer the actual TZ env var or read /etc/timezone for the IANA name
    tz_env = _os.environ.get("TZ")
    if tz_env:
        server_tz = tz_env
    else:
        try:
            with open("/etc/timezone") as f:
                server_tz = f.read().strip() or server_tz
        except OSError:
            pass

    assignment = JobAssignment(
        job_id=job.id,
        target=job.target,
        esphome_version=job.esphome_version,
        bundle_b64=bundle_b64,
        timeout_seconds=job.timeout_seconds,
        ota_only=job.ota_only,
        validate_only=job.validate_only,
        download_only=job.download_only,
        server_ota=job.server_ota,
        ota_address=job.ota_address,
        server_timezone=server_tz,
    )
    return web.json_response(assignment.model_dump(exclude_none=True))


# Matches ESPHome ProgressBar's "Uploading: [====...] NN%" frames (emitted
# via --dashboard, see _server_ota_push). Takes the last match in a chunk
# since a single read() can contain more than one \r-redrawn frame.
#
# Anchored to the "Uploading" label rather than matching a bare ``NN%``:
# esphome's output carries other percentages (RAM/flash usage summaries,
# pip progress from a lazy component install), and a loose ``(\d+)%``
# scrapes those into status_text as a bogus upload percentage.
_UPLOAD_PROGRESS_RE = re.compile(r"Uploading[^\r\n]*?(\d+)%")

# Strong references to in-flight _server_ota_push tasks. asyncio only holds
# a weak reference to a running task, so without this the garbage collector
# is free to cancel a push mid-transfer. Entries remove themselves via the
# done callback at the scheduling site.
_server_ota_tasks: set[asyncio.Task] = set()


async def _server_ota_push(app: web.Application, job: Job) -> None:
    """SOTA.2: perform server-side OTA after a server_ota compile job succeeds.

    Any worker can compile; this function runs on the server (HA host) which
    has direct access to Thread/Matter device IPv6 addresses. Reads the OTA
    binary from firmware_storage, extracts the config bundle to a temp dir,
    and runs ``esphome upload --device <addr> --file <bin> <target.yaml>``.
    Updates ota_result on the job via patch_ota_result so the Queue tab shows
    the final outcome. Fires as a fire-and-forget asyncio.Task.
    """
    queue = app["queue"]
    cfg: AppConfig = app["config"]
    job_id: str = job.id
    ota_addr: str = job.ota_address or ""
    target: str = job.target

    if not _scanner._esphome_ready.is_set() or not _scanner._server_esphome_bin:
        logger.error("Server OTA %s: ESPHome not ready on server", job_id)
        await queue.patch_ota_result(job_id, "failed")
        return
    esphome_bin: str = _scanner._server_esphome_bin

    # Only the OTA-safe shapes are valid here. "factory" is the full flash
    # image (bootloader + partition table + app at absolute offsets, meant
    # for a first flash via serial/esptool) — pushing it through the OTA
    # protocol writes the wrong bytes into the OTA partition and can fail
    # verification at the device's finalize step. "firmware" is the
    # pre-#69 legacy synthetic name for the same OTA-safe shape as "ota".
    ota_binary = (
        firmware_storage.read_firmware(job_id, variant="ota")
        or firmware_storage.read_firmware(job_id, variant="firmware")
    )
    if not ota_binary:
        logger.error(
            "Server OTA %s: no OTA-safe firmware binary found in storage "
            "(a factory-only image can't be safely pushed via OTA)", job_id,
        )
        await queue.patch_ota_result(job_id, "failed")
        return

    try:
        bundle_bytes = await create_bundle_async(cfg.config_dir, target)
    except Exception:
        logger.exception("Server OTA %s: config bundle creation failed", job_id)
        await queue.patch_ota_result(job_id, "failed")
        return

    ota_ok = False
    ota_log = ""
    try:
        with tempfile.TemporaryDirectory(prefix=f"sota-{job_id[:8]}-") as tmpdir:
            with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tf:
                # Same safer extraction as the worker's extract_bundle()
                # (client.py) — filter="data" strips path-traversal/symlink
                # tricks a malicious or corrupted bundle could otherwise use.
                try:
                    tf.extractall(tmpdir, filter="data")
                except TypeError:
                    tf.extractall(tmpdir)  # Python < 3.12

            target_yaml = Path(tmpdir) / target
            if not target_yaml.exists():
                logger.error(
                    "Server OTA %s: target %s not found in bundle", job_id, target
                )
                await queue.patch_ota_result(job_id, "failed")
                return

            ota_bin_path = target_yaml.with_suffix(".ota.bin")
            ota_bin_path.write_bytes(ota_binary)

            # Pre-flight ping to surface connectivity issues early in the log.
            # Runs on the server (HA host), not on the compile worker.
            #
            # SOTA.5: this path used to be Thread/Matter-only, where
            # ota_addr is always a resolved IPv6 literal (fd00::...) — a
            # hardcoded ping6 made sense. Now that ordinary WiFi/Ethernet
            # devices can also land here (worker-unreachable fallback),
            # ota_addr is just as often IPv4, and ping6 against an IPv4
            # address fails immediately with "Address family for hostname
            # not supported" rather than actually pinging. Pick the right
            # binary based on the address itself; a hostname (not a
            # literal IP — shouldn't normally happen since device_poller
            # always resolves to a literal, but be defensive) falls back
            # to plain ping.
            # Lazy imports — neither is module-level in api.py, and both are
            # only needed on this server-side OTA push path, not the common
            # compile-only job path most jobs take.
            import ipaddress as _ipaddress  # noqa: PLC0415
            import socket as _socket  # noqa: PLC0415
            server_hostname = _socket.gethostname()
            try:
                is_ipv6 = isinstance(_ipaddress.ip_address(ota_addr), _ipaddress.IPv6Address)
            except ValueError:
                is_ipv6 = False
            ping_cmd = ["ping6" if is_ipv6 else "ping", "-c", "3", "-W", "5", ota_addr]
            logger.info(
                "Server OTA %s: pinging %s from server (%s)",
                job_id, ota_addr, server_hostname,
            )
            try:
                ping_proc = await asyncio.create_subprocess_exec(
                    *ping_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                ping_out, _ = await asyncio.wait_for(ping_proc.communicate(), timeout=20)
                ping_log = ping_out.decode("utf-8", errors="replace") if ping_out else ""
                ping_ok = ping_proc.returncode == 0
                logger.info(
                    "Server OTA %s: ping %s — %s",
                    job_id, ota_addr, "reachable" if ping_ok else "unreachable",
                )
            except Exception as ping_exc:
                ping_log = f"ping failed: {ping_exc}"
                ping_ok = False
                logger.warning("Server OTA %s: ping error: %s", job_id, ping_exc)

            cmd = [
                esphome_bin,
                # --dashboard forces ESPHome's ProgressBar to emit periodic
                # "Uploading: [====] NN%" frames even over a piped, non-tty
                # stdout (it's a no-op otherwise). It's ESPHome's own
                # dashboard that relies on this same flag to parse live
                # progress out of a subprocess — same use case here: gives
                # the idle-timeout loop below real progress to reset
                # against during a slow transfer, and lets us surface
                # upload % in the Queue tab via status_text. This is a
                # top-level flag (registered on the main argument parser,
                # not the "upload" subcommand's) so it must come BEFORE
                # the subcommand name, not after it.
                "--dashboard",
                "upload",
                "--device", ota_addr,
                "--file", str(ota_bin_path),
                str(target_yaml),
            ]
            logger.info(
                "Server OTA %s (%s): %s", job_id, target, " ".join(cmd)
            )
            ota_log = f"--- ping {ota_addr} from server ({server_hostname}) ---\n{ping_log}\n"
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=tmpdir,
                )
                assert proc.stdout is not None  # guaranteed by stdout=PIPE above
                proc_stdout = proc.stdout
                # Progress-based timeout, not a flat total-duration bound:
                # Thread OTA can legitimately take minutes for a sub-1MB
                # image on a slow/lossy mesh. Read incrementally and reset
                # the idle clock on every chunk received (including the
                # --dashboard progress frames above) — only genuine
                # silence for SERVER_OTA_IDLE_TIMEOUT counts as stuck, with
                # SERVER_OTA_ABSOLUTE_TIMEOUT as a last-resort safety net.
                chunks: list[bytes] = []
                last_reported_pct: int | None = None
                loop = asyncio.get_running_loop()
                deadline = loop.time() + SERVER_OTA_ABSOLUTE_TIMEOUT
                stuck_reason: str | None = None
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        stuck_reason = f"exceeded the {SERVER_OTA_ABSOLUTE_TIMEOUT}s absolute cap"
                        break
                    read_timeout = min(SERVER_OTA_IDLE_TIMEOUT, remaining)
                    try:
                        chunk = await asyncio.wait_for(
                            proc_stdout.read(4096), timeout=read_timeout
                        )
                    except asyncio.TimeoutError:
                        if remaining <= SERVER_OTA_IDLE_TIMEOUT:
                            stuck_reason = f"exceeded the {SERVER_OTA_ABSOLUTE_TIMEOUT}s absolute cap"
                        else:
                            stuck_reason = (
                                f"no output for {SERVER_OTA_IDLE_TIMEOUT}s "
                                "(transfer appears stuck, not just slow)"
                            )
                        break
                    if not chunk:
                        break  # EOF — process finished producing output
                    chunks.append(chunk)

                    match = _UPLOAD_PROGRESS_RE.findall(
                        chunk.decode("utf-8", errors="replace")
                    )
                    if match and int(match[-1]) != last_reported_pct:
                        last_reported_pct = int(match[-1])
                        try:
                            await queue.update_status(job_id, f"Uploading {last_reported_pct}%")
                        except Exception:
                            logger.debug("Failed to update status_text for job %s", job_id, exc_info=True)

                collected_log = b"".join(chunks).decode("utf-8", errors="replace")
                if stuck_reason:
                    ota_ok = False
                    proc.kill()
                    await proc.wait()
                    ota_log += f"{collected_log}\nServer OTA {stuck_reason}"
                else:
                    await proc.wait()
                    ota_ok = proc.returncode == 0
                    ota_log += collected_log
            except Exception as exc:
                ota_ok = False
                ota_log += f"Server OTA subprocess error: {exc}"
                # An exception here (a bad read, a queue.update_status
                # failure propagating unexpectedly, etc.) can leave `proc`
                # still running while the surrounding `TemporaryDirectory`
                # is about to be cleaned up out from under it — it's
                # reading the OTA binary from that directory. Kill it
                # rather than let an `esphome upload` outlive its tmpdir.
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
    except Exception:
        logger.exception("Server OTA %s: unexpected error during OTA push", job_id)
        await queue.patch_ota_result(job_id, "failed")
        return

    logger.info(
        "Server OTA %s (%s): %s", job_id, target, "success" if ota_ok else "failed"
    )
    await queue.patch_ota_result(job_id, "success" if ota_ok else "failed", log=ota_log)

    if ota_ok:
        device_poller = app.get("device_poller")
        if device_poller is not None:
            try:
                asyncio.create_task(device_poller.refresh_target(target))
            except Exception:
                logger.exception(
                    "Server OTA %s: failed to schedule device refresh for %s",
                    job_id, target,
                )


@routes.post("/api/v1/jobs/{id}/result")
async def submit_job_result(request: web.Request) -> web.Response:
    job_id = request.match_info["id"]

    msg, err = await _parse_body(request, JobResultSubmission)
    if err is not None:
        return err
    assert msg is not None

    queue = request.app["queue"]
    registry = request.app["registry"]

    # Find the worker that owns this job and update registry
    job = queue.get(job_id)
    if job and job.assigned_client_id:
        registry.set_job(job.assigned_client_id, None)

    ok = await queue.submit_result(
        job_id, msg.status, msg.log, msg.ota_result,
        requires_server_ota=msg.requires_server_ota,
    )
    if not ok:
        return _protocol_error("job_not_found_or_wrong_state", status=404)

    # SOTA.2: after a server_ota compile succeeds, push OTA from the server.
    # The worker submitted ota_result=None (compile only); the server now
    # performs the actual flash using esphome upload server-side. The block
    # below (note_target_flashed / refresh_target) does not fire for this
    # submission since msg.ota_result is None here — _server_ota_push fires
    # its own refresh_target once the real OTA completes.
    #
    # download_only is checked again here even though start_compile already
    # refuses to set server_ota on such a job. This is the last gate before
    # a device actually gets written to, and the two flags arriving together
    # -- from a queue.json written by an older build, or a future caller of
    # enqueue() that doesn't replicate the rule -- must never be resolved in
    # favour of flashing.
    refreshed_job = queue.get(job_id)
    if (
        refreshed_job is not None
        and getattr(refreshed_job, "server_ota", False)
        and not getattr(refreshed_job, "download_only", False)
        and msg.status == "success"
        and refreshed_job.has_firmware
        and refreshed_job.ota_address
    ):
        try:
            # Hold a strong reference until the push finishes. The event loop
            # only keeps a weak one, so a bare create_task() can be garbage
            # collected mid-flight -- and this task legitimately runs for up
            # to SERVER_OTA_ABSOLUTE_TIMEOUT (30 min), which is a long time
            # to be collectable. Same pattern git_versioning._pending and
            # worker_log_broker use for their own long-lived tasks.
            task = asyncio.create_task(_server_ota_push(request.app, refreshed_job))
            _server_ota_tasks.add(task)
            task.add_done_callback(_server_ota_tasks.discard)
        except Exception:
            logger.exception("Failed to schedule server OTA push for job %s", job_id)

    # #11: push fresh ``running_version`` + ``compilation_time`` into the UI
    # within ~1s after a successful OTA instead of waiting up to one
    # device_poller cycle. Skip on failures and on validate-only jobs.
    # #238: stamp ``compilation_time`` server-side immediately (the firmware
    # we just flashed was compiled moments ago, so server-time is correct
    # within a few seconds), then ALSO fire ``refresh_target`` async to pick
    # up the device-reported ``running_version`` once it's back on the
    # network. The synchronous stamp means the UI updates instantly even
    # when the device is mid-reboot and the API connect is going to fail.
    if msg.status == "success" and msg.ota_result == "success" and job is not None:
        device_poller = request.app.get("device_poller")
        if device_poller is not None:
            try:
                await device_poller.note_target_flashed(job.target)
                # Don't block the response on the device-info round-trip;
                # fire-and-forget on the event loop.
                asyncio.create_task(device_poller.refresh_target(job.target))
            except Exception:
                logger.exception("Failed to schedule post-OTA device refresh for %s", job.target)

    return web.json_response(OkResponse().model_dump())


# Variant names workers may upload. Kept server-side as an authoritative
# whitelist so stale/unexpected values can't create arbitrary filenames.
# "firmware" = pre-#69 legacy shape (see firmware_storage.LEGACY_VARIANT).
_UPLOADABLE_VARIANTS = ("factory", "ota", "firmware")

# Bug #236: grace window during which firmware-variant uploads are
# accepted on a job whose state has just transitioned out of WORKING.
# Reporter saw 409 ``job_not_working`` on the SECOND variant upload of a
# successful flash because the server's timeout-checker had flipped the
# job mid-upload (slow Unraid + compose worker, ~minute-long upload of
# both factory + ota). The worker-side ordering is already correct
# (every success path uploads variants before submit_result), so the
# fix is to tolerate the late upload from the still-assigned worker
# rather than discard the binary the user can hand-flash later.
_FIRMWARE_UPLOAD_GRACE_SECONDS = 60


async def _handle_firmware_upload(
    request: web.Request, *, variant: str,
) -> web.Response:
    """Shared handler for ``POST /api/v1/jobs/{id}/firmware[/{variant}]``.

    The legacy no-variant route (pre-#69 workers) funnels through this
    with ``variant="firmware"``; the new variant-qualified route passes
    ``"factory"`` or ``"ota"``. All state-safety checks (bug #24,
    security audit F-08) run identically for both shapes.
    """
    job_id = request.match_info["id"]
    queue = request.app["queue"]
    caller_client_id = (
        request.headers.get(HEADER_X_CLIENT_ID)
        or request.rel_url.query.get("client_id")
    )

    if variant not in _UPLOADABLE_VARIANTS:
        return _protocol_error("unknown_firmware_variant", status=400)

    job = queue.get(job_id)
    if job is None:
        return _protocol_error("job_not_found", status=404)
    # Bug #9 (1.6.1): firmware uploads are accepted for every job kind,
    # not just ``download_only``. The worker now post-OTA uploads the
    # compiled binary so the server archives a downloadable artifact
    # for every successful compile — useful for forensics, rollback,
    # and hand-flashing devices whose OTA path is broken. Download-only
    # jobs still have ``has_firmware`` set the same way; the state
    # check below remains the real gate (only accept while WORKING).

    # #24 (1): state check MUST run before any disk write. A stale
    # worker (the one that was abandoned by bug #17's offline
    # short-circuit) will fail this check and we refuse without
    # touching disk.
    from job_queue import JobState, _utcnow  # noqa: PLC0415
    in_grace_window = False
    if job.state != JobState.WORKING:
        # Bug #236: tolerate a late variant upload from the still-assigned
        # worker if the job's terminal transition was within the grace
        # window. This handles the slow-worker race where the server's
        # timeout-checker flips the job mid-upload of variant 2.
        finished_at = job.finished_at
        now = _utcnow()
        in_grace_window = (
            finished_at is not None
            and (now - finished_at).total_seconds() <= _FIRMWARE_UPLOAD_GRACE_SECONDS
            and caller_client_id is not None
            and job.assigned_client_id == caller_client_id
        )
        if not in_grace_window:
            logger.info(
                "Refusing firmware upload for job %s (variant=%s) — state is %s "
                "(stale worker %s; current assigned: %s)",
                job_id, variant, job.state.value, caller_client_id, job.assigned_client_id,
            )
            return _protocol_error("job_not_working", status=409)
        logger.info(
            "Accepting late firmware upload for job %s (variant=%s) — state is %s "
            "but worker %s is still the assigned worker and finished_at is within "
            "%ds grace window (#236)",
            job_id, variant, job.state.value, caller_client_id,
            _FIRMWARE_UPLOAD_GRACE_SECONDS,
        )

    # #24 (2) / security audit F-08: worker identity must match the
    # currently-assigned worker. Without this, an abandoned-then-late
    # worker could still overwrite the successor's upload.
    if (
        caller_client_id
        and job.assigned_client_id
        and caller_client_id != job.assigned_client_id
    ):
        logger.warning(
            "Refusing firmware upload for job %s (variant=%s) — caller %s is not the "
            "assigned worker %s (stale upload after requeue?)",
            job_id, variant, caller_client_id, job.assigned_client_id,
        )
        return _protocol_error("worker_identity_mismatch", status=409)

    data = await request.read()
    if not data:
        return _protocol_error("empty_firmware_body", status=400)

    from firmware_storage import save_firmware, delete_firmware  # noqa: PLC0415
    try:
        save_firmware(job_id, data, variant=variant)
    except Exception:
        logger.exception(
            "Failed to save firmware for job %s (variant=%s)", job_id, variant,
        )
        return _protocol_error("firmware_save_failed", status=500)

    if in_grace_window:
        # Bug #236: terminal job; skip the WORKING-only mark and just
        # set has_firmware so the UI/firmware-reconciler doesn't sweep
        # the file we just wrote. Job state stays terminal — the worker
        # will follow up with submit_result (which is also tolerant)
        # or has already done so.
        await queue.mark_firmware_stored_force(job_id)
        return web.json_response(OkResponse().model_dump())

    ok = await queue.mark_firmware_stored(job_id)
    if not ok:
        # Genuine race: the job transitioned out of WORKING between
        # our pre-write state check and mark_firmware_stored. Clean
        # up EVERY variant written by this worker for this job — the
        # queue rejected the whole job, not just one variant.
        logger.info(
            "Cleaned up out-of-order firmware upload for job %s (variant=%s) "
            "(transitioned out of WORKING during write)", job_id, variant,
        )
        delete_firmware(job_id)
        return _protocol_error("job_not_eligible", status=409)

    return web.json_response(OkResponse().model_dump())


@routes.post("/api/v1/jobs/{id}/firmware")
async def upload_job_firmware_legacy(request: web.Request) -> web.Response:
    """Legacy no-variant route (#69) — pre-#69 workers still hit this.

    Stored as variant ``firmware`` (the pre-#69 blob name). Once the
    worker auto-updates to the post-#69 client, subsequent uploads go
    through ``/firmware/{variant}`` with the real variant name.
    """
    return await _handle_firmware_upload(request, variant="firmware")


@routes.post("/api/v1/jobs/{id}/firmware/{variant}")
async def upload_job_firmware(request: web.Request) -> web.Response:
    """FD.5 (#69-extended) — worker uploads one variant of the compiled binary.

    ``variant`` ∈ {``factory``, ``ota``}: ESP32 produces both; ESP8266
    only produces ``ota``. Workers call this once per variant, so a
    single job may carry multiple binaries in ``/data/firmware/``.
    Body: raw bytes of the ``.bin``. All other semantics match the
    legacy route — see ``_handle_firmware_upload``.
    """
    variant = request.match_info["variant"]
    return await _handle_firmware_upload(request, variant=variant)


@routes.post("/api/v1/jobs/{id}/status")
async def update_job_status(request: web.Request) -> web.Response:
    job_id = request.match_info["id"]

    msg, err = await _parse_body(request, JobStatusUpdate)
    if err is not None:
        return err
    assert msg is not None

    queue = request.app["queue"]
    ok = await queue.update_status(job_id, msg.status_text)
    if not ok:
        return _protocol_error("job_not_found", status=404)
    return web.json_response(OkResponse().model_dump())


@routes.get("/api/v1/client/version")
async def get_client_version(request: web.Request) -> web.Response:
    return web.json_response({"version": _get_server_client_version()})


@routes.get("/api/v1/client/code")
async def get_client_code(request: web.Request) -> web.Response:
    """Return all .py files from the bundled worker directory.

    Gated on image version: workers with a stale Docker image are refused,
    because source-code updates alone can't fix a stale image and would
    just cause them to repeatedly exec into broken state.
    """
    # Look up the caller's image version from the registry
    client_id = request.headers.get(HEADER_X_CLIENT_ID) or request.rel_url.query.get("client_id")
    if client_id:
        worker = request.app["registry"].get(client_id)
        if worker and not _image_version_ok(worker.image_version):
            return web.json_response(
                {
                    "error": "image_upgrade_required",
                    "min_image_version": MIN_IMAGE_VERSION,
                    "reported": worker.image_version,
                },
                status=409,
            )

    base = _CLIENT_CODE_DIR if _CLIENT_CODE_DIR.exists() else Path(__file__).parent
    files = {}
    for path in sorted(base.glob("*.py")):
        if path.name.startswith("._"):
            continue
        try:
            files[path.name] = path.read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed to read worker file %s", path.name)
    return web.json_response({
        "version": _get_server_client_version(),
        "files": files,
    })


@routes.post("/api/v1/jobs/{id}/log")
async def append_job_log(request: web.Request) -> web.Response:
    """Append streaming log lines from a build worker (HTTP batched)."""
    job_id = request.match_info["id"]

    # C.3: refuse oversized log uploads at the request boundary so a single
    # huge POST body never gets buffered into RAM before the in-function cap
    # in JobQueue.append_log fires. We use 4× MAX_LOG_BYTES (≈2 MB) as the
    # ceiling — generous enough that legitimate batched flushes are never
    # rejected, but tight enough to prevent a malicious or buggy worker from
    # parking gigabytes in the parser.
    from job_queue import MAX_LOG_BYTES  # noqa: PLC0415
    max_body = MAX_LOG_BYTES * 4
    content_length = request.content_length
    if content_length is not None and content_length > max_body:
        return _protocol_error(
            "log_payload_too_large",
            f"body {content_length} bytes exceeds cap {max_body}",
            status=413,
        )

    msg, err = await _parse_body(request, JobLogAppend)
    if err is not None:
        return err
    assert msg is not None

    queue = request.app["queue"]
    ok = await queue.append_log(job_id, msg.lines)
    if not ok:
        return _protocol_error("job_not_found", status=404)

    # Forward to any browser WebSocket subscribers
    subscribers: dict = request.app.get("log_subscribers", {})
    for sub_ws in list(subscribers.get(job_id, set())):
        try:
            await sub_ws.send_str(msg.lines)
        except Exception:
            subscribers[job_id].discard(sub_ws)

    return web.json_response(OkResponse().model_dump())


@routes.get("/api/v1/workers/{id}/control")
async def get_worker_control(request: web.Request) -> web.Response:
    """WL.2: fast-path control signal the worker polls at 1 Hz.

    Heartbeat also carries ``stream_logs`` but runs every 10 s, which
    makes the dialog feel dead for up to 10 s after opening before the
    first line appears. This endpoint is cheap — just the broker's
    is_watched() boolean — so the worker can poll it every second
    without the traffic cost of a full heartbeat. Bearer auth (same
    middleware as the rest of ``/api/v1/*``).
    """
    client_id = request.match_info["id"]
    broker = request.app.get("worker_log_broker")
    stream_logs = broker.is_watched(client_id) if broker else False
    body: dict[str, object] = {"stream_logs": stream_logs}
    # #109: piggyback any outstanding diagnostics request on the same
    # 1-Hz poll so the worker reacts within a second of a UI click.
    diag = request.app.get("diagnostics_broker")
    if diag is not None:
        pending = diag.pending_for_worker(client_id)
        if pending is not None:
            body["diagnostics_request_id"] = pending
    return web.json_response(body)


@routes.post("/api/v1/workers/{id}/logs")
async def append_worker_log(request: web.Request) -> web.Response:
    """WL.2: receive a worker-log push while ``stream_logs`` is on.

    Body-size cap + shape mirror the job-log path (``/api/v1/jobs/{id}/log``)
    so the two log streams look identical on the wire. The heartbeat
    handler is what flips ``stream_logs``; this endpoint just trusts
    the flag and buffers what arrives.
    """
    client_id = request.match_info["id"]

    from job_queue import MAX_LOG_BYTES  # noqa: PLC0415
    max_body = MAX_LOG_BYTES * 4
    content_length = request.content_length
    if content_length is not None and content_length > max_body:
        return _protocol_error(
            "log_payload_too_large",
            f"body {content_length} bytes exceeds cap {max_body}",
            status=413,
        )

    msg, err = await _parse_body(request, WorkerLogAppend)
    if err is not None:
        return err
    assert msg is not None

    broker = request.app.get("worker_log_broker")
    if broker is None:
        # Shouldn't happen in production (main.py instantiates it in
        # app startup), but guard so tests that wire only the registry
        # don't crash with a vaguer error.
        return _protocol_error("worker_log_broker_unavailable", status=500)

    await broker.append_async(client_id, msg.offset, msg.lines)
    return web.json_response(OkResponse().model_dump())


@routes.post("/api/v1/workers/{id}/diagnostics")
async def receive_worker_diagnostics(request: web.Request) -> web.Response:
    """#109: receive a py-spy thread dump a worker produced in response
    to an outstanding diagnostics request.

    The body is the typed :class:`WorkerDiagnosticsUpload` — ``request_id``
    matches the id the server handed out on heartbeat / control; ``ok``
    distinguishes a real dump from an error message; ``dump`` carries
    either. Stored in the diagnostics broker keyed by ``request_id`` so
    the UI's polling endpoint can hand it back.
    """
    client_id = request.match_info["id"]

    # Cap the body size so a misbehaving worker can't push an
    # unbounded string. py-spy dumps are well under 2 MB in practice.
    from diagnostics import MAX_UPLOAD_BYTES  # noqa: PLC0415
    content_length = request.content_length
    if content_length is not None and content_length > MAX_UPLOAD_BYTES:
        return _protocol_error(
            "diagnostics_payload_too_large",
            f"body {content_length} bytes exceeds cap {MAX_UPLOAD_BYTES}",
            status=413,
        )

    msg, err = await _parse_body(request, WorkerDiagnosticsUpload)
    if err is not None:
        return err
    assert msg is not None

    diag = request.app.get("diagnostics_broker")
    if diag is None:
        return _protocol_error("diagnostics_broker_unavailable", status=500)

    diag.store_result(msg.request_id, ok=msg.ok, dump=msg.dump)
    diag.claim_pending(client_id, msg.request_id)
    return web.json_response(OkResponse().model_dump())


@routes.get("/api/v1/jobs/{id}/log/ws")
async def ws_worker_log(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for build workers to stream log lines."""
    job_id = request.match_info["id"]
    queue = request.app["queue"]
    job = queue.get(job_id)
    if not job:
        return web.json_response({"error": "Job not found"}, status=404)  # type: ignore[return-value]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    subscribers: dict = request.app.setdefault("log_subscribers", {})
    # The worker WS is a producer; it is not added to subscribers

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await queue.append_log(job_id, msg.data)
            for sub_ws in list(subscribers.get(job_id, set())):
                try:
                    await sub_ws.send_str(msg.data)
                except Exception:
                    subscribers[job_id].discard(sub_ws)
        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
            break

    return ws


@routes.get("/api/v1/status")
async def get_status(request: web.Request) -> web.Response:
    registry = request.app["registry"]
    queue = request.app["queue"]
    from settings import get_settings  # noqa: PLC0415
    threshold = get_settings().worker_offline_threshold

    online_workers = sum(
        1 for w in registry.get_all() if registry.is_online(w.client_id, threshold)
    )

    return web.json_response(
        {
            "esphome_version": get_esphome_version(),
            "online_clients": online_workers,  # kept for backwards compatibility
            "online_workers": online_workers,
            "queue_size": queue.queue_size(),
        }
    )


# Schema version for the /metrics/queue payload (#168). Bump if a field is
# removed or its meaning changes — additive fields don't need a bump.
METRICS_QUEUE_SCHEMA_VERSION = 1


@routes.get("/api/v1/metrics/queue")
async def get_metrics_queue(request: web.Request) -> web.Response:
    """#168: machine-readable queue-depth metric for external autoscalers.

    Compatible with KEDA's ``metrics-api`` scaler (JSONPath ``$.active``),
    HPA via the ``external`` metrics adapter, Sablier, or any custom
    controller. Bearer auth via the existing /api/v1 middleware — same
    trust boundary as the worker-claim endpoints (read-only metric).

    Response shape (1.7.2-dev.6):

    .. code-block:: json

        {
          "pending": 2,
          "working": 1,
          "active": 3,
          "online_workers": 1,
          "max_parallel_capacity": 2,
          "schema_version": 1
        }

    - ``active = pending + working`` — the metric the autoscaler should
      scale on. Pending alone undercounts a mid-compile run; working
      alone undercounts a backlog the existing workers can't catch up
      on at parallelism = 1.
    - ``max_parallel_capacity`` = ``sum(w.max_parallel_jobs)`` across
      online workers — lets a controller answer "do existing online
      workers have headroom?" before spinning a new pod/VM/LXC.
    - ``schema_version`` is monotonic; downstream consumers can pin a
      version and refuse to read newer payloads if a future bump
      removes a field they depend on.
    """
    registry = request.app["registry"]
    queue = request.app["queue"]
    from settings import get_settings  # noqa: PLC0415
    threshold = get_settings().worker_offline_threshold

    pending = 0
    working = 0
    for j in queue.get_all():
        if j.state == JobState.PENDING:
            pending += 1
        elif j.state == JobState.WORKING:
            working += 1

    online_workers = 0
    max_parallel_capacity = 0
    for w in registry.get_all():
        if registry.is_online(w.client_id, threshold):
            online_workers += 1
            max_parallel_capacity += max(0, int(w.max_parallel_jobs or 0))

    return web.json_response({
        "pending": pending,
        "working": working,
        "active": pending + working,
        "online_workers": online_workers,
        "max_parallel_capacity": max_parallel_capacity,
        "schema_version": METRICS_QUEUE_SCHEMA_VERSION,
    })
