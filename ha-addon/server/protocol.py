"""Typed server↔worker protocol messages (pydantic v2).

This module is the single source of truth for the shapes of all JSON payloads
that cross the `/api/v1/*` boundary between the server and build workers.

Canonical copy lives in `ha-addon/server/protocol.py`; an identical copy is
kept at `ha-addon/client/protocol.py` because the server and client Docker
build contexts cannot share files. The two files MUST remain byte-identical;
a CI check enforces this.

Design notes:
- ``model_config = ConfigDict(extra="ignore")`` on every model: an older peer
  receiving a payload from a newer peer silently drops unknown fields rather
  than failing validation. This is the forward-compatibility story for
  adding new optional fields in future protocol versions.
- Missing required fields are rejected with a 400 ``ProtocolError``. New
  required fields require a ``PROTOCOL_VERSION`` bump.
- ``PROTOCOL_VERSION`` is bumped on breaking changes. The server and client
  both send their protocol version on register + heartbeat; the server
  rejects unknown versions cleanly instead of half-processing them.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Protocol version — bump on any breaking change to these models.
# ---------------------------------------------------------------------------

PROTOCOL_VERSION: int = 1
"""Current wire protocol version.

Bump this integer whenever a breaking change is made to any message in this
file: required field added, field type narrowed, field removed. Additive
optional fields do NOT require a bump because of ``extra="ignore"``.
"""


# ---------------------------------------------------------------------------
# Base class — shared config for every message.
# ---------------------------------------------------------------------------


class _ProtocolMessage(BaseModel):
    """Base class with the shared pydantic config.

    ``extra="ignore"`` gives forward compatibility: a deployed worker that
    receives a response with new fields from a newer server (or vice versa)
    silently drops the unknown keys instead of crashing with a validation
    error. New optional fields can be rolled out without a protocol bump.
    """

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Embedded payloads
# ---------------------------------------------------------------------------


class SystemInfo(_ProtocolMessage):
    """Hardware/OS information sent by the worker on register + heartbeat.

    Every field is Optional because collection is best-effort on the client;
    a minimal environment or a failed psutil call must not break registration.
    """

    cpu_arch: Optional[str] = None
    os_version: Optional[str] = None
    cpu_cores: Optional[int] = None
    cpu_model: Optional[str] = None
    total_memory: Optional[str] = None
    uptime: Optional[str] = None
    perf_score: Optional[int] = None
    cpu_usage: Optional[float] = None
    disk_total: Optional[str] = None
    disk_free: Optional[str] = None
    disk_used_pct: Optional[int] = None
    # DQ.6: worker's view of the disk-quota engine's most recent state.
    # ``disk_usage_bytes`` is the engine's measured byte total under
    # ``/esphome-versions/`` (venvs + caches + slots + pio-slots).
    # ``disk_quota_bytes`` is the effective quota the worker is enforcing
    # against — usually identical to whatever the server most recently
    # pushed via ``HeartbeatResponse.set_disk_quota_bytes`` but kept here
    # so the UI sees what the worker is actually using rather than what
    # the server thinks it pushed. ``last_eviction_freed_bytes`` is the
    # bytes freed by the most recent post-job sweep — useful for the UI
    # to surface "evicted N bytes" toasts.
    disk_usage_bytes: Optional[int] = None
    disk_quota_bytes: Optional[int] = None
    last_eviction_freed_bytes: Optional[int] = None


# ---------------------------------------------------------------------------
# Register — POST /api/v1/workers/register
# ---------------------------------------------------------------------------


class RegisterRequest(_ProtocolMessage):
    hostname: str
    platform: str
    client_version: Optional[str] = None
    image_version: Optional[str] = None
    client_id: Optional[str] = None  # for re-register across restarts
    max_parallel_jobs: int = 1
    system_info: Optional[SystemInfo] = None
    # TG.1: worker tags. ``tags`` is the value of WORKER_TAGS (comma-split,
    # trimmed) on the worker. The server seeds its persistent store from this
    # on the *first* registration for the worker's identity (hostname, falling
    # back to client_id); subsequent registrations are server-side-wins so a UI
    # tag edit isn't clobbered by the next worker restart. ``overwrite_tags``
    # (set when the worker has WORKER_TAGS_OVERWRITE=1) restores the old "env
    # always wins" behaviour for scripted multi-worker deployments.
    tags: Optional[list[str]] = None
    overwrite_tags: bool = False
    # DQ.6: worker's boot-time disk-quota override (from the
    # ``WORKER_DISK_QUOTA_GB`` env var, converted to bytes by the
    # client). Server seeds the persistent override from this on the
    # *first* registration for the identity (mirrors the ``tags`` flow);
    # subsequent registrations ignore it (server-side wins). ``None``
    # means "no override; use the fleet default."
    disk_quota_bytes: Optional[int] = None
    protocol_version: int = PROTOCOL_VERSION


class RegisterResponse(_ProtocolMessage):
    client_id: str
    protocol_version: int = PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Heartbeat — POST /api/v1/workers/heartbeat
# ---------------------------------------------------------------------------


class HeartbeatRequest(_ProtocolMessage):
    client_id: str
    system_info: Optional[SystemInfo] = None
    protocol_version: int = PROTOCOL_VERSION


class HeartbeatResponse(_ProtocolMessage):
    ok: bool = True
    server_client_version: Optional[str] = None
    image_upgrade_required: Optional[bool] = None
    min_image_version: Optional[str] = None
    set_max_parallel_jobs: Optional[int] = None
    # DQ.6: server's effective per-worker disk-quota in bytes (override
    # from WorkerDiskQuotaStore ?? AppSettings.default_worker_disk_quota_bytes).
    # The worker stores this in a thread-safe cell and the engine reads
    # it on every sweep, so a UI edit propagates within one heartbeat
    # without restarting the worker. ``None`` is unused on the wire today
    # (server always sends a value), but kept Optional for the same
    # forward-compat reasons as set_max_parallel_jobs.
    set_disk_quota_bytes: Optional[int] = None
    clean_build_cache: Optional[bool] = None
    # WL.2: None = "unchanged" (default — older servers never set it).
    # True = start pushing logs at 1 Hz to /api/v1/workers/{id}/logs.
    # False = stop pushing and tear down the pusher thread.
    stream_logs: Optional[bool] = None
    # #109: when non-empty, the server is asking the worker to produce a
    # py-spy thread dump of its own process and POST it back to
    # /api/v1/workers/{id}/diagnostics. The worker is free to dedupe on
    # the id so repeated heartbeats under one outstanding request don't
    # trigger multiple dumps.
    diagnostics_request_id: Optional[str] = None
    protocol_version: int = PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Deregister — POST /api/v1/workers/deregister
# ---------------------------------------------------------------------------


class DeregisterRequest(_ProtocolMessage):
    client_id: str


class OkResponse(_ProtocolMessage):
    ok: bool = True


# ---------------------------------------------------------------------------
# Jobs — GET /api/v1/jobs/next, POST /api/v1/jobs/{id}/result, /status, /log
# ---------------------------------------------------------------------------


class JobAssignment(_ProtocolMessage):
    """Server's response body for GET /api/v1/jobs/next when a job is available."""

    job_id: str
    target: str
    esphome_version: str
    bundle_b64: str
    timeout_seconds: int = 600
    ota_only: bool = False
    validate_only: bool = False
    # FD.1: when true the worker runs `esphome compile` (not `esphome run`),
    # skips the OTA upload, and instead POSTs the compiled binary back to
    # the server via /api/v1/jobs/{id}/firmware. The user downloads it
    # later from the Queue tab.
    download_only: bool = False
    # SOTA.1: when true the worker compiles (like download_only) and uploads
    # the binary; the server then performs OTA push via `esphome upload`.
    # Used for Thread/Matter devices only reachable from the HA host.
    # Optional + False default keeps older workers forward-compatible.
    server_ota: bool = False
    ota_address: Optional[str] = None
    server_timezone: Optional[str] = None


class JobResultSubmission(_ProtocolMessage):
    status: Literal["success", "failed"]
    log: Optional[str] = None
    ota_result: Optional[str] = None


class JobStatusUpdate(_ProtocolMessage):
    status_text: str = ""


class JobLogAppend(_ProtocolMessage):
    lines: str = ""


# ---------------------------------------------------------------------------
# Worker logs — POST /api/v1/workers/{client_id}/logs (WL.2, pull-when-watched)
# ---------------------------------------------------------------------------


class WorkerLogAppend(_ProtocolMessage):
    """Payload the worker sends while the server's stream_logs flag is on.

    Shape mirrors ``JobLogAppend`` as closely as possible — the same pre-
    formatted, ANSI-coloured, newline-terminated text xterm renders directly.
    ``offset`` is the byte-offset of the first byte of ``lines`` since worker
    process start; the server uses it to dedupe retries and to detect worker
    restarts (``offset`` going backwards).
    """

    offset: int = 0
    lines: str = ""


# ---------------------------------------------------------------------------
# Diagnostics — POST /api/v1/workers/{client_id}/diagnostics (#109)
# ---------------------------------------------------------------------------


class WorkerDiagnosticsUpload(_ProtocolMessage):
    """Worker's reply after a server-initiated diagnostics request.

    ``request_id`` matches the token the server handed out via
    ``HeartbeatResponse.diagnostics_request_id`` / the control endpoint, so
    a UI client polling for the result can correlate upload to request.

    ``dump`` is the ``py-spy dump --pid <self>`` text output (or, when py-spy
    attach fails on the worker host — missing binary, dropped SYS_PTRACE —
    a short human-readable error string the UI surfaces verbatim). ``ok``
    distinguishes the two so the UI can render a download vs. a failure
    toast without parsing text.
    """

    request_id: str
    ok: bool = True
    dump: str = ""


# ---------------------------------------------------------------------------
# Error envelope — returned by the server on validation failures (HTTP 400).
# ---------------------------------------------------------------------------


class ProtocolError(_ProtocolMessage):
    """Structured error body returned on protocol validation failures.

    Mirrors the existing ad-hoc ``{"error": "..."}`` shape so consumers that
    only read ``error`` continue to work, and adds optional diagnostic fields
    for machines (``reason``, ``protocol_version``).
    """

    error: str
    reason: Optional[str] = None
    protocol_version: Optional[int] = Field(default=PROTOCOL_VERSION)
