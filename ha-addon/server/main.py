"""aiohttp application entry point for Fleet for ESPHome — see CHANGELOG for the rebrand history."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import IO, Optional

import aiohttp
from aiohttp import web

import api as api_module
import ui_api as ui_api_module
from app_config import AppConfig
from device_poller import DevicePoller
from job_queue import JobQueue
from registry import WorkerRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Suppress noisy per-request access logs (heartbeats, polls, UI refreshes)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
# Suppress aioesphomeapi connection warnings (expected when devices are offline)
logging.getLogger("aioesphomeapi.connection").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

@web.middleware
async def version_header_middleware(request: web.Request, handler):
    """Attach X-Server-Version header to every response for UI change detection."""
    response = await handler(request)
    from api import _get_server_client_version  # noqa: PLC0415
    from constants import HEADER_X_SERVER_VERSION  # noqa: PLC0415
    response.headers[HEADER_X_SERVER_VERSION] = _get_server_client_version()
    return response


# E.9: defence-in-depth security headers on every UI-tier response. Applied
# to ``/``, ``/index.html``, ``/assets/*``, and every ``/ui/api/*`` endpoint
# (the browser-facing surface). NOT applied to ``/api/v1/*`` since those are
# consumed programmatically by build workers and the headers add no value.
#
# CSP design notes:
# - script-src needs 'unsafe-inline' because Tailwind v4 generates
#   inline styles at runtime and @monaco-editor/react's loader still
#   injects a small script tag for worker bootstrap even under the local
#   bundle config.
# - style-src needs 'unsafe-inline' for the same Tailwind + Monaco reason.
# - connect-src must allow wss: for the live-log WebSocket and
#   https://schema.esphome.io for the editor schema fetcher (api/esphomeSchema.ts).
# - worker-src 'self' blob: covers Monaco's editor worker.
# - frame-ancestors 'self' enforces clickjacking protection without breaking
#   HA Ingress (which loads us in an iframe served from the same origin).
#
# CF.1: ``cdn.jsdelivr.net`` is NO LONGER allowed. Monaco now ships
# bundled into the app via ``src/monaco-local.ts`` and
# ``loader.config({ monaco })`` — the @monaco-editor/react wrapper no
# longer fetches runtime/worker scripts from jsDelivr at editor-open
# time, so the CDN origin was dropped from every CSP directive. The
# editor now works in air-gapped installs and survives a jsDelivr
# outage as a side-benefit. Any regression that tries to re-add
# ``cdn.jsdelivr.net`` to any directive is caught by
# ``tests/test_security_headers.py::test_csp_has_no_jsdelivr``.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss: https://schema.esphome.io; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "accelerometer=(), camera=(), geolocation=(), microphone=(), payment=(), usb=()",
    "X-Frame-Options": "SAMEORIGIN",  # legacy fallback for browsers that ignore CSP frame-ancestors
}


@web.middleware
async def compression_middleware(request: web.Request, handler):
    """SP.1: opportunistic gzip on UI JSON responses.

    Scope: /ui/api/* responses that are plain `web.Response` (what
    `web.json_response()` returns). A typical /ui/api/targets response on
    a 50-device fleet is ~40-50 KB; gzip cuts it to ~5-10 KB. Adds up
    across 1 Hz polls for devices/queue/workers over slow uplinks (HA
    Ingress from mobile / VPN).

    Explicitly skipped:
      - /api/v1/* (worker tier). Worker↔server runs on a local network
        and the job-claim response carries a base64-encoded tarball of
        the whole config dir (~46 MB for a full ESPHome workspace);
        synchronously gzipping that blocks the event loop and saves
        little on a LAN.
      - FileResponse (static/). aiohttp's file handler has its own
        Range/cache/compression logic that conflicts with
        enable_compression's `assert self._payload_writer is not None`.
      - WebSocketResponse, status 204/304, and empty-body responses —
        nothing meaningful to compress, and aiohttp's second assert
        `self._body is not None` fires on them otherwise.
    """
    response = await handler(request)
    if not request.path.startswith("/ui/api/"):
        return response
    if type(response) is not web.Response:
        return response
    if response.headers.get("Content-Encoding"):
        return response
    if response.status in (204, 304):
        return response
    body = response.body
    if body is None:
        return response
    if isinstance(body, (bytes, bytearray)) and len(body) == 0:
        return response
    try:
        response.enable_compression()
    except Exception:
        logger.debug("enable_compression failed; sending uncompressed", exc_info=True)
    return response


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """Attach defence-in-depth security headers to UI-tier responses (E.9)."""
    response = await handler(request)
    path = request.path
    if path.startswith("/api/v1/"):
        # Worker tier — no benefit, skip.
        return response
    # Everything else (UI HTML, static assets, /ui/api/*, /, /index.html)
    # gets the headers. WebSocket upgrades (101 Switching Protocols) inherit
    # them too, which is harmless.
    for k, v in _SECURITY_HEADERS.items():
        # Don't clobber a header an inner handler explicitly set.
        if k not in response.headers:
            response.headers[k] = v
    return response


def _normalize_peer_ip(raw: str) -> str:
    """Canonicalize a peer IP string for comparison against HA_SUPERVISOR_IP.

    Strips IPv4-mapped IPv6 prefixes (``::ffff:172.30.32.2`` → ``172.30.32.2``)
    and zone identifiers (``fe80::1%eth0`` → ``fe80::1``), so an IPv4 string
    in HA_SUPERVISOR_IP still matches the same supervisor coming in over a
    dual-stack socket. Falls back to the raw string if parsing fails — that
    way an unparseable address simply won't match the supervisor (which is
    safer than crashing the request).
    """
    if not raw:
        return ""
    # Strip IPv6 zone id (e.g. ``fe80::1%eth0``)
    raw = raw.split("%", 1)[0]
    try:
        import ipaddress  # noqa: PLC0415
        addr = ipaddress.ip_address(raw)
        # IPv4-mapped IPv6 → unwrap to plain IPv4 string
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(addr)
    except (ValueError, ImportError):
        return raw


# Bug #7: rate-limiting state for the auth-failure WARNING emitter.
# Log once per (peer_ip, reason) pair per AUTH_FAIL_LOG_WINDOW_SECONDS,
# and then again with a summary count of suppressed lines when the
# next real log fires — so operators still see the pattern without
# the raw line-per-request torrent (hass-4 saw ~14k/hour).
AUTH_FAIL_LOG_WINDOW_SECONDS = 60.0
_auth_fail_last_logged: dict[tuple[str, str], float] = {}
_auth_fail_suppressed: dict[tuple[str, str], int] = {}


def _log_auth_failure(path: str, reason: str, peer_ip: str) -> None:
    """Throttled WARNING emitter for /api/v1/* auth failures."""
    from constants import HA_SUPERVISOR_IP  # noqa: PLC0415
    now = time.monotonic()
    key = (peer_ip or "<unknown>", reason)
    last = _auth_fail_last_logged.get(key, 0.0)
    elapsed = now - last
    if elapsed < AUTH_FAIL_LOG_WINDOW_SECONDS:
        _auth_fail_suppressed[key] = _auth_fail_suppressed.get(key, 0) + 1
        return
    suppressed = _auth_fail_suppressed.pop(key, 0)
    tail = f" ({suppressed} similar suppressed in last {int(elapsed)}s)" if suppressed else ""
    logger.warning(
        "401 on %s: reason=%s peer_ip=%s (expected supervisor=%s)%s",
        path, reason, peer_ip or "<unknown>", HA_SUPERVISOR_IP, tail,
    )
    _auth_fail_last_logged[key] = now


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path

    # /ui/api/* — no auth; HA handles ingress authentication
    if path.startswith("/ui/api/") or path in ("/", "/index.html"):
        return await handler(request)

    # /api/v1/* — require Bearer token UNLESS from HA supervisor address
    if path.startswith("/api/v1/"):
        # ``transport`` may be None during tests or for some edge-case
        # transports; ``peername`` may also be None even on a real transport
        # (e.g. unix-socket connections, closed streams). Handle both without
        # crashing — fall through to token auth in either case. C.2.
        peer_ip = ""
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
        except Exception:
            peer = None
        if peer:
            raw_ip = peer[0] if isinstance(peer, tuple) else str(peer)
            peer_ip = _normalize_peer_ip(raw_ip)

        from constants import HA_SUPERVISOR_IP, HEADER_AUTHORIZATION  # noqa: PLC0415
        # Normalize the configured Supervisor IP too so an IPv6-mapped form
        # like ``::ffff:172.30.32.2`` from a dual-stack Docker network still
        # matches the canonical IPv4 string. Compare canonicalized forms.
        if peer_ip and peer_ip == _normalize_peer_ip(HA_SUPERVISOR_IP):
            return await handler(request)

        # SP.8: read the token live from Settings, so flipping it in
        # the drawer propagates to the next request with no restart.
        from settings import get_settings  # noqa: PLC0415
        token = get_settings().server_token
        if token:
            from helpers import constant_time_compare  # noqa: PLC0415
            auth_header = request.headers.get(HEADER_AUTHORIZATION, "")
            if auth_header.startswith("Bearer ") and constant_time_compare(auth_header[7:], token):
                return await handler(request)

            # Diagnose the refusal. Each branch logs a distinct structured
            # reason so operators can tell "wrong token" from "missing header"
            # from "non-supervisor peer IP" without enabling debug logging.
            # Rate-limited per (peer_ip, reason) via _log_auth_failure (#7).
            if not auth_header:
                reason = "missing_authorization_header"
            elif not auth_header.startswith("Bearer "):
                reason = "authorization_not_bearer_scheme"
            else:
                reason = "bearer_token_mismatch"
            _log_auth_failure(path, reason, peer_ip or "")
        else:
            # No token configured — allow all (development mode)
            logger.warning("No auth token configured; allowing unauthenticated request to %s", path)
            return await handler(request)

        return web.json_response({"error": "Unauthorized"}, status=401)

    # Everything else — pass through
    return await handler(request)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def timeout_checker(app: web.Application) -> None:
    """Background task: check for timed-out jobs every 30 seconds.

    Bug #18: no longer auto-prunes terminal jobs. The queue is the
    user's record of what the system did; time-based pruning used to
    erase overnight compile history before the user had a chance to
    see it. Users clear the queue explicitly via the Queue tab's Clear
    dropdown.
    """
    queue: JobQueue = app["queue"]
    registry: WorkerRegistry = app["registry"]
    while True:
        await asyncio.sleep(30)
        try:
            # Bug #17: short-circuit re-queue for jobs whose worker went
            # offline mid-job. Previously these sat stalled for the full
            # JOB_TIMEOUT (600s default) before the elapsed-time check
            # noticed. Now check_timeouts also re-queues WORKING jobs
            # whose assigned worker has no recent heartbeat.
            #
            # SP.8: both the offline-threshold probe and the job-timeout
            # value read live from Settings each iteration, so Settings
            # drawer edits take effect on the next 30s tick.
            from settings import get_settings  # noqa: PLC0415
            s = get_settings()

            def _is_online(cid: str) -> bool:
                return registry.is_online(cid, threshold_secs=s.worker_offline_threshold)

            timed_out = await queue.check_timeouts(is_worker_online=_is_online)
            if timed_out:
                logger.info("Timeout checker: processed %d timed-out jobs", len(timed_out))
        except Exception:
            logger.exception("Error in timeout checker")


async def firmware_retention_enforcer(app: web.Application) -> None:
    """Bug #198 (extending #38): evict firmware binaries on age first,
    then on disk-budget pressure.

    Two passes per tick:

    1. **Time-based retention** — delete every `.bin` whose mtime is
       older than ``firmware_retention_days`` (default 2). Live-queue
       jobs are protected; *history-protected binaries are NOT exempt*
       (a successful compile from last week shouldn't pin a binary on
       disk for a year just because the history row still exists).
    2. **Budget-based eviction (#38)** — if on-disk usage still exceeds
       ``firmware_cache_max_gb``, evict oldest-mtime first. Both live
       queue and history-protected binaries are kept here; this pass
       is a hard ceiling against catastrophic growth between retention
       ticks.

    First tick 90 s after startup (lets reconcile_orphans run first
    and the queue settle), then every 30 min. PR #64 review: pre-fix
    we only protected ``queue.get_all()``, which silently evicted
    downloads older than 30 minutes — fixed by unioning with history
    rows whose binary is still on disk for the budget pass.
    """
    queue = app.get("queue")
    if queue is None:
        return
    first = True
    while True:
        await asyncio.sleep(90 if first else 30 * 60)
        first = False
        try:
            from settings import get_settings  # noqa: PLC0415
            from firmware_storage import enforce_budget, enforce_retention  # noqa: PLC0415
            settings = get_settings()
            # Live-queue jobs are protected from both passes.
            queue_protected: set[str] = {
                job.id for job in queue.get_all()
                if getattr(job, "has_firmware", False)
            }

            # ---- Pass 1: time-based retention (#198) ------------------
            # Only the live-queue is protected here; we deliberately
            # evict history-protected binaries when they fall out of
            # the retention window so the disk doesn't accumulate a
            # year of unused .bins. Setting <= 0 disables this pass.
            retention_days = int(getattr(settings, "firmware_retention_days", 0) or 0)
            if retention_days > 0:
                evicted = enforce_retention(
                    max_age_seconds=retention_days * 86400,
                    protected_job_ids=queue_protected,
                )
                if evicted:
                    logger.info(
                        "Firmware retention enforcer: evicted %d file(s) older than %d day(s)",
                        evicted, retention_days,
                    )

            # ---- Pass 2: budget hard-ceiling (#38) --------------------
            gb = float(getattr(settings, "firmware_cache_max_gb", 0.0) or 0.0)
            max_bytes = int(gb * 1024 * 1024 * 1024)
            if max_bytes <= 0:
                continue
            # Budget pass also protects history-row binaries — bug #9
            # (1.6.1) made every successful compile keep an archived
            # binary, so we LRU-evict only when disk pressure forces it.
            protected = set(queue_protected)
            history = app.get("job_history")
            if history is not None:
                try:
                    offset = 0
                    page = 1000
                    while True:
                        rows = history.query(state="success", limit=page, offset=offset)
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
            deleted = enforce_budget(max_bytes=max_bytes, protected_job_ids=protected)
            if deleted:
                logger.info(
                    "Firmware budget enforcer: evicted %d file(s) (limit %.2f GiB)",
                    deleted, gb,
                )
        except Exception:
            logger.exception("Error in firmware_retention_enforcer loop")


async def job_history_retention(app: web.Application) -> None:
    """JH.3: evict job-history rows older than Settings' retention window.

    Runs once a day. Reads ``job_history_retention_days`` from Settings
    on every tick so drawer edits take effect on the next run without
    a server restart (0 disables retention; the DAO treats ``days <= 0``
    as a no-op). Does its first tick one minute after startup so a
    fresh boot doesn't stall while the migration/init dance runs.
    """
    history = app.get("job_history")
    if history is None:
        return
    first = True
    while True:
        await asyncio.sleep(60 if first else 24 * 60 * 60)
        first = False
        try:
            from settings import get_settings  # noqa: PLC0415
            from firmware_storage import delete_firmware  # noqa: PLC0415
            days = int(getattr(get_settings(), "job_history_retention_days", 0) or 0)
            evicted_ids = history.evict_older_than(days)
            if evicted_ids:
                # Bug #198: free the firmware `.bin` in the same tick the
                # row goes — pre-fix, the binary lingered until the next
                # add-on restart's reconcile_orphans sweep.
                bin_freed = sum(1 for jid in evicted_ids if delete_firmware(jid))
                logger.info(
                    "Job-history retention: evicted %d row(s) older than %d day(s) (freed %d firmware `.bin`)",
                    len(evicted_ids), days, bin_freed,
                )
        except Exception:
            logger.exception("Error in job_history_retention loop")


async def ha_entity_poller(app: web.Application) -> None:
    """Background task: poll HA entity registry every 30s to determine which
    ESPHome devices are configured in Home Assistant and whether they are
    currently connected.

    Requires SUPERVISOR_TOKEN (injected automatically when hassio_api: true).
    Stores results in app["_rt"]["ha_entity_status"]: dict[str, {configured, connected}]
    keyed by normalised device name (hyphens replaced with underscores, lowercase).
    """
    import os  # noqa: PLC0415

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        logger.info("No SUPERVISOR_TOKEN — HA entity status polling disabled")
        return

    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=10)
    first_poll = True

    # Repeated identical failures are demoted to DEBUG after the second
    # occurrence of the same fingerprint, so a persistent outage (HA down,
    # network blip) doesn't drown the log and mask unrelated problems. A
    # single iteration can emit multiple warnings with different fingerprints
    # (e.g. both "template_exception" and "poll_exception") and each is
    # counted independently. Any successful poll clears all counts.
    warning_counts: dict[str, int] = {}

    def _log_poll_warning(fingerprint: str, message: str, *args: object, exc_info: bool = False) -> None:
        count = warning_counts.get(fingerprint, 0) + 1
        warning_counts[fingerprint] = count
        if count <= 2:
            logger.warning(message, *args, exc_info=exc_info)
            if count == 2:
                logger.warning(
                    "Above warning is repeating; further identical failures "
                    "will be logged at DEBUG level until the next success."
                )
        else:
            logger.debug(message, *args, exc_info=exc_info)

    def _reset_poll_warnings() -> None:
        warning_counts.clear()

    while True:
        # Poll immediately on first iteration, then every 30s
        if not first_poll:
            await asyncio.sleep(30)
        try:
            import json as _json  # noqa: PLC0415

            async with aiohttp.ClientSession() as session:
                # 1. Use the template API to get ALL ESPHome entity IDs.
                #    This works even for devices without a _status sensor.
                esphome_entity_ids: list[str] = []
                try:
                    async with session.post(
                        "http://supervisor/core/api/template",
                        headers={**headers, "Content-Type": "application/json"},
                        json={"template": "{{ integration_entities('esphome') | list | tojson }}"},
                        timeout=timeout,
                    ) as resp:
                        if resp.status == 200:
                            raw = await resp.text()
                            try:
                                parsed = _json.loads(raw)
                                esphome_entity_ids = parsed if isinstance(parsed, list) else []
                            except (_json.JSONDecodeError, TypeError):
                                _log_poll_warning(
                                    "template_unparseable",
                                    "HA template API returned unparseable response: %.200s", raw,
                                )
                        else:
                            body = await resp.text()
                            # #11: 5xx is almost always HA Core or the
                            # Supervisor proxy bouncing; suggest the right
                            # remedy instead of pointing at the (correctly-set)
                            # homeassistant_api flag.
                            hint = (
                                " — HA Core may be restarting; will retry on the next poll"
                                if 500 <= resp.status < 600 else
                                " — check homeassistant_api: true in config.yaml"
                                if resp.status in (401, 403) else
                                ""
                            )
                            _log_poll_warning(
                                f"template_http_{resp.status}",
                                "HA template API returned HTTP %d%s: %.200s", resp.status, hint, body,
                            )
                except Exception:
                    _log_poll_warning(
                        "template_exception",
                        "Template API call failed", exc_info=True,
                    )

                # 1b. Get MAC + device_id + entity_id for ESPHome devices via template API.
                # ESPHome devices store MACs in device connections (not identifiers):
                #   connections = [["mac", "50:02:91:3c:11:43"]]
                # #35: we also grab the HA device_id so the UI can build a
                # deep-link to /config/devices/device/<id>.
                # #41: returns one row per entity so we can build
                # entity_id→device_id AND name→device_id fallbacks for
                # offline devices where the poller may not have a MAC.
                ha_mac_set: set[str] = set()
                ha_mac_to_device_id: dict[str, str] = {}
                entity_to_device_id: dict[str, str] = {}
                if esphome_entity_ids:
                    # Query 1: MAC + device_id, deduped by device (small output)
                    try:
                        mac_tmpl = (
                            "{%- set ns = namespace(pairs=[], seen=[]) -%}"
                            "{%- for eid in integration_entities('esphome') -%}"
                            "  {%- set did = device_id(eid) -%}"
                            "  {%- if did and did not in ns.seen -%}"
                            "    {%- set ns.seen = ns.seen + [did] -%}"
                            "    {%- set conns = device_attr(did, 'connections') -%}"
                            "    {%- if conns -%}"
                            "      {%- for conn in conns -%}"
                            "        {%- if conn[0] == 'mac' -%}"
                            "          {%- set ns.pairs = ns.pairs + [[conn[1], did]] -%}"
                            "        {%- endif -%}"
                            "      {%- endfor -%}"
                            "    {%- endif -%}"
                            "  {%- endif -%}"
                            "{%- endfor -%}"
                            "{{ ns.pairs | tojson }}"
                        )
                        async with session.post(
                            "http://supervisor/core/api/template",
                            headers={**headers, "Content-Type": "application/json"},
                            json={"template": mac_tmpl},
                            timeout=timeout,
                        ) as resp:
                            if resp.status == 200:
                                raw = await resp.text()
                                try:
                                    parsed = _json.loads(raw)
                                    if isinstance(parsed, list):
                                        for item in parsed:
                                            if isinstance(item, list) and len(item) == 2:
                                                mac_lc = str(item[0]).lower()
                                                did_str = str(item[1])
                                                ha_mac_set.add(mac_lc)
                                                ha_mac_to_device_id[mac_lc] = did_str
                                except (_json.JSONDecodeError, TypeError):
                                    pass
                    except Exception:
                        logger.debug("MAC template query failed", exc_info=True)

                    # Query 2: entity_id → device_id mapping (for name-based fallback)
                    try:
                        eid_tmpl = (
                            "{%- set ns = namespace(pairs=[]) -%}"
                            "{%- for eid in integration_entities('esphome') -%}"
                            "  {%- set did = device_id(eid) -%}"
                            "  {%- if did -%}"
                            "    {%- set ns.pairs = ns.pairs + [[eid, did]] -%}"
                            "  {%- endif -%}"
                            "{%- endfor -%}"
                            "{{ ns.pairs | tojson }}"
                        )
                        async with session.post(
                            "http://supervisor/core/api/template",
                            headers={**headers, "Content-Type": "application/json"},
                            json={"template": eid_tmpl},
                            timeout=timeout,
                        ) as resp:
                            if resp.status == 200:
                                raw = await resp.text()
                                try:
                                    parsed = _json.loads(raw)
                                    if isinstance(parsed, list):
                                        for item in parsed:
                                            if isinstance(item, list) and len(item) == 2:
                                                entity_to_device_id[str(item[0])] = str(item[1])
                                except (_json.JSONDecodeError, TypeError):
                                    pass
                    except Exception:
                        logger.debug("Entity→device_id template query failed", exc_info=True)

                # 2. Fetch states for connectivity info
                async with session.get(
                    "http://supervisor/core/api/states",
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        # #11: differentiate the hint by status class so 5xx
                        # (HA Core / Supervisor proxy bouncing) doesn't tell
                        # the user to "check homeassistant_api" when their
                        # config is fine.
                        hint = (
                            " — HA Core may be restarting; will retry on the next poll"
                            if 500 <= resp.status < 600 else
                            " — check homeassistant_api: true in config.yaml"
                            if resp.status in (401, 403) else
                            ""
                        )
                        _log_poll_warning(
                            f"states_http_{resp.status}",
                            "HA states returned HTTP %d%s", resp.status, hint,
                        )
                        continue
                    states: list[dict] = await resp.json()

            # Build connectivity map from binary_sensor.*_status with device_class=connectivity
            connectivity: dict[str, bool] = {}  # norm_name → connected
            for entity in states:
                entity_id: str = entity.get("entity_id", "")
                if not entity_id.startswith("binary_sensor.") or not entity_id.endswith("_status"):
                    continue
                attrs = entity.get("attributes") or {}
                if attrs.get("device_class") != "connectivity":
                    continue
                norm_name = entity_id[len("binary_sensor."):-len("_status")]
                connectivity[norm_name] = entity.get("state") == "on"

            # Build ha_status from ESPHome entity IDs (configured) + connectivity
            ha_status: dict[str, dict] = {}

            # Extract unique device name prefixes from entity IDs.
            # ESPHome entities follow the pattern: <domain>.<device_name>_<entity_suffix>
            # We collect all unique prefixes by stripping the domain and finding the
            # longest prefix that matches a connectivity key, or the full local part.
            # #41: also build a norm_name → device_id map so offline devices
            # (which may not have a MAC in the poller) can still get a deep link.
            esphome_device_names: set[str] = set()
            ha_name_to_device_id: dict[str, str] = {}
            for eid in esphome_entity_ids:
                if "." not in eid:
                    continue
                local = eid.split(".", 1)[1]  # e.g. "nespresso_machine_temperature"
                # Check if any connectivity key is a prefix of this entity
                matched_name: str | None = None
                for conn_name in connectivity:
                    if local == conn_name or local.startswith(conn_name + "_"):
                        esphome_device_names.add(conn_name)
                        matched_name = conn_name
                        break
                if matched_name is None:
                    # No connectivity match — try to derive device name from entity ID.
                    # The _status entity would be the definitive prefix, but it may not
                    # exist. Store the full local as a candidate; _ha_status_for_target
                    # will match by prefix.
                    esphome_device_names.add(local)
                    matched_name = local
                # Record device_id keyed by the name prefix we'll use for matching.
                did = entity_to_device_id.get(eid)
                if did and matched_name not in ha_name_to_device_id:
                    ha_name_to_device_id[matched_name] = did

            # All connectivity-matched devices get configured=True + connected state
            for name in connectivity:
                ha_status[name] = {"configured": True, "connected": connectivity[name]}

            # All other ESPHome entities mark their device as configured (connected unknown)
            for name in esphome_device_names:
                if name not in ha_status:
                    ha_status[name] = {"configured": True, "connected": None}

            app["_rt"]["ha_entity_status"].clear()
            app["_rt"]["ha_entity_status"].update(ha_status)
            app["_rt"]["ha_mac_set"] = ha_mac_set
            app["_rt"]["ha_mac_to_device_id"] = ha_mac_to_device_id
            app["_rt"]["ha_name_to_device_id"] = ha_name_to_device_id

            # A full successful poll resets the suppression state so the next
            # transient failure gets its warning re-promoted.
            _reset_poll_warnings()

            if first_poll:
                first_poll = False
                configured_count = len(ha_status)
                connected_count = sum(1 for v in ha_status.values() if v.get("connected") is not None)
                logger.info(
                    "HA entity poller: %d ESPHome entities from template API, "
                    "%d devices configured, %d with connectivity status",
                    len(esphome_entity_ids),
                    configured_count,
                    connected_count,
                )
            else:
                logger.debug(
                    "HA entity status updated: %d ESPHome devices",
                    len(ha_status),
                )

        except Exception:
            _log_poll_warning(
                "poll_exception",
                "Error polling HA entity status", exc_info=True,
            )
        finally:
            # Always clear first_poll so subsequent retries sleep 30s,
            # even when the first attempt fails with an exception or a
            # non-200 status (which uses `continue` to restart the loop).
            first_poll = False


async def reseed_device_poller_from_config(app: web.Application, *, reason: str) -> None:
    """Bug #11 (1.6.1): re-run ``build_name_to_target_map`` and re-seed
    the device poller's name_map, encryption_keys, and address overrides.

    The startup sequence calls ``build_name_to_target_map`` before the
    ESPHome venv has finished lazy-installing; ``_resolve_esphome_config``
    returns ``None`` during that window, which means
    ``api.encryption.key`` (and ``esphome.name`` substitutions) never
    make it into the poller. This helper is invoked when a meaningful
    event makes the resolver likely to succeed — the ESPHome install
    completes, the Supervisor-reported version changes — so the poller
    catches up without waiting for a config-file change to trigger the
    normal 30-second config-scanner re-run.

    *reason* is a short string logged alongside the reseed so operators
    can trace which trigger fired.

    Runs on a thread executor because ``build_name_to_target_map`` calls
    ESPHome's full validator per target (#84), which is CPU-bound
    (voluptuous schemas + component tree) and at ~67 targets blocks the
    event loop for tens of seconds on a shared-core HAOS box. Long
    enough to trip Supervisor's healthcheck and trigger a container
    restart. Keeping the work off the event loop is what makes the
    server stay reachable during a rescan.
    """
    from scanner import scan_configs, build_name_to_target_map  # noqa: PLC0415
    cfg: AppConfig = app["config"]
    device_poller = app.get("device_poller")
    if device_poller is None:
        return
    try:
        loop = asyncio.get_running_loop()
        targets = await loop.run_in_executor(None, scan_configs, cfg.config_dir)
        name_map, enc_keys, addr_overrides, addr_sources = await loop.run_in_executor(
            None, build_name_to_target_map, cfg.config_dir, targets,
        )
        device_poller.update_compile_targets(
            targets, name_map, enc_keys, addr_overrides, addr_sources,
        )
        logger.info(
            "Device poller reseeded from config (%s): %d targets, %d encryption keys",
            reason, len(targets), len(enc_keys),
        )
    except Exception:
        logger.exception("Failed to reseed device poller from config (%s)", reason)


async def config_scanner(app: web.Application) -> None:
    """Background task: re-scan config dir every 30s and update device poller targets.

    ``build_name_to_target_map`` runs in an executor because it calls
    ESPHome's full validator per target (#84), which is CPU-bound and,
    at ~67 targets, blocks long enough to trip Supervisor's container
    healthcheck if run on the event loop.
    """
    from scanner import scan_configs, build_name_to_target_map  # noqa: PLC0415

    cfg: AppConfig = app["config"]
    device_poller = app.get("device_poller")
    prev_targets: list[str] = []
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(30)
        try:
            targets = await loop.run_in_executor(None, scan_configs, cfg.config_dir)
            if targets != prev_targets:
                logger.info("Config change detected: %d targets (was %d)", len(targets), len(prev_targets))
                if device_poller:
                    name_map, enc_keys, addr_overrides, addr_sources = await loop.run_in_executor(
                        None, build_name_to_target_map, cfg.config_dir, targets,
                    )
                    device_poller.update_compile_targets(targets, name_map, enc_keys, addr_overrides, addr_sources)
                prev_targets = targets
        except Exception:
            logger.exception("Error in config scanner")


async def _legacy_schedule_checker_removed() -> None:
    """Removed in #87 — replaced by APScheduler in scheduler.py."""


async def schedule_checker(app: web.Application) -> None:
    """LEGACY — kept as a no-op stub so tests that import it don't break.

    The real scheduler is now APScheduler in scheduler.py, started via
    scheduler.start(app) in on_startup.
    """
    return


async def _old_schedule_checker(app: web.Application) -> None:
    """Background task: check per-device cron schedules (SU.7 hardened).

    SU.7 improvements over the original fixed-60s-tick approach:
    - Next-fire-driven sleep: computes the earliest next-fire across all
      schedules and sleeps until then (capped at 60s so config changes
      land promptly). Eliminates up-to-60s latency.
    - Misfire grace window (300s default): if a schedule was missed by more
      than the grace window (e.g. server was down for hours), it's skipped
      with a log warning rather than fired late.
    - History ring buffer: records the last 50 fire events per target in
      ``schedule_history`` for the SU.6 history view.
    """
    import uuid as _uuid  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    import schedule_history  # noqa: PLC0415
    from scanner import scan_configs, read_device_meta, write_device_meta, get_esphome_version  # noqa: PLC0415

    try:
        from croniter import croniter  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        logger.warning("croniter not installed — per-device scheduling disabled")
        return

    cfg: AppConfig = app["config"]
    queue: JobQueue = app["queue"]
    misfire_grace = getattr(cfg, "misfire_grace_seconds", 300)

    app["_rt"]["schedule_checker_started_at"] = datetime.now(timezone.utc).isoformat()
    app["_rt"]["schedule_checker_tick_count"] = 0
    app["_rt"]["schedule_checker_last_tick"] = None
    app["_rt"]["schedule_checker_last_error"] = None

    def _get_ota_address(target: str) -> str | None:
        # Bug #18 (1.6.1): route through ``resolve_ota_address`` so the
        # static_ip vs. mDNS vs. ``.local`` precedence stays consistent
        # with scheduler + ui_api. The old inline expression preferred
        # the override over the real IP even when the override was a
        # stale ``.local`` fallback; the helper picks the best IP
        # literal available.
        device_poller = app.get("device_poller")
        if not device_poller:
            return None
        for dev in device_poller.get_devices():
            if dev.compile_target == target and dev.ip_address:
                return device_poller.resolve_ota_address(dev.name)
        return None

    next_sleep = 5.0  # first tick after 5s
    logger.info("schedule_checker started (config_dir=%s)", cfg.config_dir)

    while True:
        await asyncio.sleep(next_sleep)
        next_fires: list[datetime] = []
        try:
            targets = scan_configs(cfg.config_dir)
            now = datetime.now(timezone.utc)
            app["_rt"]["schedule_checker_tick_count"] += 1
            app["_rt"]["schedule_checker_last_tick"] = now.isoformat()

            for target in targets:
                try:
                    meta = read_device_meta(cfg.config_dir, target)

                    # --- One-time schedule ---
                    once_str = meta.get("schedule_once")
                    if once_str:
                        try:
                            once_dt = datetime.fromisoformat(once_str)
                            if once_dt.tzinfo is None:
                                once_dt = once_dt.replace(tzinfo=timezone.utc)
                            if once_dt > now:
                                next_fires.append(once_dt)
                            elif (now - once_dt).total_seconds() <= misfire_grace:
                                version = meta.get("pin_version") or get_esphome_version()
                                run_id = str(_uuid.uuid4())
                                from settings import get_settings  # noqa: PLC0415
                                from git_versioning import get_head  # noqa: PLC0415
                                job = await queue.enqueue(
                                    target=target,
                                    esphome_version=version,
                                    run_id=run_id,
                                    timeout_seconds=get_settings().job_timeout,
                                    ota_address=_get_ota_address(target),
                                    config_hash=get_head(Path(cfg.config_dir)),
                                )
                                if job is not None:
                                    job.scheduled = True
                                    schedule_history.record(target, now, job.id)
                                    logger.info("One-time schedule fired for %s (at=%s): job %s", target, once_str, job.id)
                                fresh_meta = read_device_meta(cfg.config_dir, target)
                                fresh_meta.pop("schedule_once", None)
                                write_device_meta(cfg.config_dir, target, fresh_meta)
                            else:
                                logger.warning(
                                    "One-time schedule for %s missed by %ds (grace=%ds) — clearing",
                                    target, int((now - once_dt).total_seconds()), misfire_grace,
                                )
                                fresh_meta = read_device_meta(cfg.config_dir, target)
                                fresh_meta.pop("schedule_once", None)
                                write_device_meta(cfg.config_dir, target, fresh_meta)
                            continue
                        except Exception:
                            logger.exception("One-time schedule parse failed for %s", target)

                    # --- Recurring schedule ---
                    cron_expr = meta.get("schedule")
                    enabled = meta.get("schedule_enabled", False)
                    if not cron_expr or not enabled:
                        continue

                    last_run_str = meta.get("schedule_last_run")
                    if last_run_str:
                        last_run = datetime.fromisoformat(last_run_str)
                        if last_run.tzinfo is None:
                            last_run = last_run.replace(tzinfo=timezone.utc)
                    else:
                        last_run = now

                    cron = croniter(cron_expr, last_run)
                    next_run = cron.get_next(datetime)
                    if next_run.tzinfo is None:
                        next_run = next_run.replace(tzinfo=timezone.utc)

                    if next_run > now:
                        next_fires.append(next_run)
                        continue

                    # SU.7: misfire grace — skip if missed by more than grace
                    missed_by = (now - next_run).total_seconds()
                    if missed_by > misfire_grace:
                        logger.warning(
                            "Schedule for %s missed by %ds (grace=%ds) — skipping",
                            target, int(missed_by), misfire_grace,
                        )
                        fresh_meta = read_device_meta(cfg.config_dir, target)
                        fresh_meta["schedule_last_run"] = now.isoformat()
                        write_device_meta(cfg.config_dir, target, fresh_meta)
                        continue

                    from settings import get_settings  # noqa: PLC0415
                    from git_versioning import get_head  # noqa: PLC0415
                    version = meta.get("pin_version") or get_esphome_version()
                    run_id = str(_uuid.uuid4())
                    job = await queue.enqueue(
                        target=target,
                        esphome_version=version,
                        run_id=run_id,
                        timeout_seconds=get_settings().job_timeout,
                        ota_address=_get_ota_address(target),
                        config_hash=get_head(Path(cfg.config_dir)),
                    )
                    if job is not None:
                        job.scheduled = True
                        schedule_history.record(target, now, job.id)
                        logger.info(
                            "Schedule fired for %s (cron=%s): job %s (version=%s)",
                            target, cron_expr, job.id, version,
                        )

                    fresh_meta = read_device_meta(cfg.config_dir, target)
                    fresh_meta["schedule_last_run"] = now.isoformat()
                    write_device_meta(cfg.config_dir, target, fresh_meta)

                except Exception:
                    logger.exception("Schedule check failed for %s", target)

        except Exception as e:
            app["_rt"]["schedule_checker_last_error"] = f"{type(e).__name__}: {e}"
            logger.exception("Error in schedule checker")

        # SU.7: next-fire-driven sleep — sleep until the earliest upcoming
        # fire time, capped at 60s so config changes land promptly.
        if next_fires:
            now_after = datetime.now(timezone.utc)
            earliest = min(next_fires)
            next_sleep = max(1.0, min(60.0, (earliest - now_after).total_seconds()))
        else:
            next_sleep = 60.0


# ---------------------------------------------------------------------------
# ESPHome version detection and PyPI version list
# ---------------------------------------------------------------------------

_PYPI_CACHE_TTL = 3600  # seconds


async def _fetch_ha_esphome_version(session: aiohttp.ClientSession) -> Optional[str]:
    """Query the HA Supervisor for the installed ESPHome add-on version.

    Returns the version string, or None if not running inside an HA add-on or
    if the request fails for any reason.
    """
    import os  # noqa: PLC0415
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None

    # Two-tier discovery:
    #
    # 1. Preferred: list all installed add-ons via ``GET /addons`` and pick
    #    the ESPHome one. This works for any slug, including the hashed forms
    #    that confused the original hardcoded list. BUT it requires
    #    ``hassio_role: manager`` — plain ``hassio_api: true`` only grants
    #    access to ``/addons/<slug>/info``, and the listing returns 403.
    #    We do NOT escalate the role just for version detection; instead we
    #    silently fall back to step 2 on any non-200.
    #
    # 2. Fallback: probe a known list of slug patterns against
    #    ``/addons/<slug>/info``. This is the pre-1.3.1 mechanism plus the
    #    community-repo hash ``a0d7b954_esphome`` (added per bug #4 triage).
    #    It still misses fully-custom hashed slugs but covers ~all real
    #    installs without requiring an elevated role.
    auth = {"Authorization": f"Bearer {token}"}

    # #86: skip the tier-1 /addons listing entirely. It requires
    # hassio_role: manager which we don't have (hassio_api: true only grants
    # per-slug access). The 403 response was spamming the Supervisor log
    # with "Invalid token for access /addons" every 30s. The per-slug
    # fallback below covers all real installs.

    # --- Tier 2: per-slug probe over known patterns.
    candidate_slugs = (
        "core_esphome",
        "local_esphome",
        "a0d7b954_esphome",  # community repo (default for most users)
        "5c53de3b_esphome",  # alternate community repo hash
    )
    for slug in candidate_slugs:
        try:
            async with session.get(
                f"http://supervisor/addons/{slug}/info",
                headers=auth,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    info = await resp.json()
                    version = info.get("data", {}).get("version")
                    if version:
                        logger.debug(
                            "Detected HA ESPHome add-on version %s (slug: %s, via /info probe)",
                            version, slug,
                        )
                        return str(version)
                # 404 is the expected "not this slug" outcome — keep probing
                # silently. Anything else is unexpected but also not worth
                # spamming the log every 30s.
        except Exception as exc:
            logger.debug("Supervisor /addons/%s/info query failed: %s", slug, exc)

    return None


_PRE_RELEASE_ORDER = {"dev": -4, "a": -3, "b": -2, "rc": -1}


def _esphome_version_key(v: str) -> tuple:
    """PEP440-ish sort key for ESPHome version strings.

    Each dot-separated segment is parsed into ``(main_num, stage_rank,
    stage_num)``: the leading integer, a pre-release tier (dev < a < b <
    rc < stable, encoded as -4/-3/-2/-1/0), and the integer that follows
    the tier tag (e.g. ``b3`` → stage_num 3). Stable segments are
    ``(N, 0, 0)``.

    Keyed uniformly per segment so the tuple shapes always match,
    guaranteeing:
      - ``b3 > b2`` (bug #16 regression: earlier version discarded the
        number after the pre-release tag, so b3 and b2 produced the same
        key and relative order fell back to Python's stable sort, which
        preserved PyPI's alphabetical input order — putting b2 *above*
        b3 in descending sort).
      - Stable outranks any pre-release with the same base (``2026.3.0 >
        2026.3.0b3``). Early implementations dropped the stage tuple for
        bare-digit segments, so the stable key was a strict prefix of
        the pre-release key and sorted *below* it.
    """
    parts: list[tuple] = []
    for seg in v.split("."):
        if seg.isdigit():
            parts.append((int(seg), 0, 0))
            continue
        # e.g. "0b3" → main=0, tag="b", stage_num=3
        m = re.match(r"(\d+)(.*)", seg)
        if not m:
            # Non-numeric segment (e.g. "dev") — keep it lexicographic.
            # Cast to avoid mixed-type tuple comparisons downstream.
            parts.append((0, 0, hash(seg)))
            continue
        main_num = int(m.group(1))
        suffix = m.group(2).lower()
        for tag, rank in _PRE_RELEASE_ORDER.items():
            if suffix.startswith(tag):
                tail = suffix[len(tag):]
                stage_num = int(tail) if tail.isdigit() else 0
                parts.append((main_num, rank, stage_num))
                break
        else:
            parts.append((main_num, 0, 0))
    return tuple(parts)


async def _fetch_pypi_versions(session: aiohttp.ClientSession) -> list[str]:
    """Return ALL ESPHome versions from PyPI, newest first.

    #69: no longer capped at 50 — returns every release so users can
    access historic versions. The UI filters betas via a toggle (#64).
    """
    try:
        async with session.get(
            "https://pypi.org/pypi/esphome/json",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                releases = list(data.get("releases", {}).keys())
                releases.sort(key=_esphome_version_key, reverse=True)
                return releases
    except Exception:
        logger.debug("Failed to fetch PyPI esphome versions", exc_info=True)
    return []


# Bug #30: used by the standalone-Docker fallback to pick "latest stable"
# from a PyPI version list. Stable ESPHome versions are pure digit-and-dot
# strings ("2024.3.0"); pre-releases carry letters ("2024.3.0b1", "…rc1").
_STABLE_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


def _pick_latest_stable_version(versions: list[str]) -> Optional[str]:
    """Return the first stable entry from a newest-first list, or None."""
    for v in versions:
        if _STABLE_VERSION_RE.match(v):
            return v
    return None


async def _install_esphome_initial(app: web.Application) -> None:
    """SE.2 + bug #30 + bug #105: one-shot ESPHome lazy install at startup.

    Resolution order when there's no bundled version:
      1. Bundled package (test harness / pre-SE.1): the caller already
         set the version via `set_esphome_version`; this function is a
         no-op because `_get_installed_esphome_version` returns a real
         version.
      2. HA Supervisor: if SUPERVISOR_TOKEN is set AND the HA ESPHome
         builder add-on is installed, pin to its version. The
         `pypi_version_refresher` loop still picks up later version
         changes.
      3. PyPI latest stable: fresh HAOS without the builder add-on, or
         standalone Docker. Without this, the user is stuck on
         "Installing ESPHome…" forever (bug #105) because the refresher
         only fires when the builder add-on is present.

    Runs in an executor so it never blocks aiohttp startup.
    """
    from scanner import (  # noqa: PLC0415
        _get_installed_esphome_version,
        ensure_esphome_installed,
        set_esphome_version,
    )

    target = _get_installed_esphome_version()
    if target in ("unknown", "installing"):
        supervisor_version: Optional[str] = None
        if os.environ.get("SUPERVISOR_TOKEN"):
            try:
                async with aiohttp.ClientSession() as session:
                    supervisor_version = await _fetch_ha_esphome_version(session)
            except Exception:
                logger.exception("Bug #105: Supervisor version probe raised")
        if supervisor_version:
            target = supervisor_version
            logger.info(
                "Installing ESPHome %s from HA Supervisor (ESPHome builder add-on)",
                target,
            )
            set_esphome_version(target)
        else:
            try:
                async with aiohttp.ClientSession() as session:
                    versions = await _fetch_pypi_versions(session)
            except Exception:
                logger.exception("Bug #30: PyPI version fetch raised")
                versions = []
            picked = _pick_latest_stable_version(versions)
            if picked is None:
                logger.warning(
                    "No bundled ESPHome, no HA Supervisor version, "
                    "and PyPI lookup returned no stable versions. "
                    "UI will keep showing 'Installing ESPHome…'; "
                    "user must pick a version manually once network "
                    "access comes back (#105)."
                )
                return
            target = picked
            logger.info(
                "Installing latest stable ESPHome from PyPI: %s "
                "(no bundled version, no HA ESPHome builder add-on detected)",
                target,
            )
            set_esphome_version(target)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ensure_esphome_installed, target)
    # Bug #11 (1.6.1): encryption keys / address overrides got built
    # during the install window and every target whose YAML needs
    # substitution-pass resolution returned None. Now that the venv is
    # ready, reseed the poller so live logs + OTA can actually reach
    # encrypted devices.
    await reseed_device_poller_from_config(app, reason="esphome install complete")


async def pypi_version_refresher(app: web.Application) -> None:
    """Background task: refresh PyPI versions hourly and re-check HA ESPHome add-on every 30s.

    Runs immediately on first iteration so that the HA Supervisor version and
    the PyPI version list are populated shortly after startup, without blocking
    the server startup path.
    """
    check_interval = 30   # check HA add-on version every 30 seconds
    pypi_countdown = 0    # fetch PyPI immediately on first loop
    first_run = True
    while True:
        # Run immediately on first iteration, then every 30s.
        # first_run is set to False before the try block so that a failure on
        # the first attempt still causes subsequent attempts to sleep.
        if not first_run:
            await asyncio.sleep(check_interval)
        first_run = False
        try:
            async with aiohttp.ClientSession() as session:
                # Re-check HA ESPHome add-on version
                new_detected = await _fetch_ha_esphome_version(session)
                old_detected = app["_rt"].get("esphome_detected_version")
                if new_detected and new_detected != old_detected:
                    app["_rt"]["esphome_detected_version"] = new_detected
                    from scanner import set_esphome_version, ensure_esphome_installed  # noqa: PLC0415
                    set_esphome_version(new_detected)
                    if old_detected is None:
                        logger.info("ESPHome add-on version detected: %s", new_detected)
                    else:
                        logger.info("ESPHome add-on version changed: %s → %s", old_detected, new_detected)
                    # SE.4: lazy-install the newly-detected version in the
                    # background so the server's venv tracks whatever the HA
                    # ESPHome add-on is on. VersionManager is a fast cache
                    # hit if the version is already installed.
                    # Bug #11 (1.6.1): schedule a task that awaits the
                    # install and then reseeds the device poller — the
                    # old fire-and-forget never came back, so a version
                    # bump right after boot would leave encryption keys
                    # unpopulated until a config-file change triggered a
                    # rescan.
                    async def _install_and_reseed(ver: str) -> None:
                        loop_inner = asyncio.get_running_loop()
                        try:
                            await loop_inner.run_in_executor(
                                None, ensure_esphome_installed, ver,
                            )
                        except Exception:
                            logger.exception(
                                "SE.4: ensure_esphome_installed(%s) raised", ver,
                            )
                            return
                        await reseed_device_poller_from_config(
                            app, reason=f"esphome version change → {ver}",
                        )

                    asyncio.create_task(_install_and_reseed(new_detected))

                # Refresh PyPI list periodically
                pypi_countdown -= check_interval
                if pypi_countdown <= 0:
                    pypi_countdown = _PYPI_CACHE_TTL
                    versions = await _fetch_pypi_versions(session)
                    if versions:
                        old_count = len(app["_rt"]["esphome_available_versions"])
                        app["_rt"]["esphome_available_versions"] = versions
                        app["_rt"]["esphome_versions_fetched_at"] = time.monotonic()
                        if old_count == 0 or len(versions) != old_count:
                            logger.info("Refreshed PyPI ESPHome version list: %d versions", len(versions))
                        else:
                            logger.debug("Refreshed PyPI ESPHome version list: %d versions (unchanged)", len(versions))
        except Exception:
            logger.exception("Error in version refresher")


# ---------------------------------------------------------------------------
# Static file serving with ingress path injection
# ---------------------------------------------------------------------------

async def serve_index(request: web.Request) -> web.Response:
    """Serve index.html with X-Ingress-Path base href injection."""
    try:
        html = INDEX_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        return web.Response(status=404, text="index.html not found")

    from constants import HEADER_X_INGRESS_PATH  # noqa: PLC0415
    ingress_path = request.headers.get(HEADER_X_INGRESS_PATH, "")
    if ingress_path:
        # SA.1 / F-15: defence-in-depth. The header is Supervisor-supplied
        # on the HA happy path, but we're injecting it directly into the
        # `<base href="...">` attribute, so strip anything that isn't a
        # URL-safe path character before interpolation. Keeps `"`/`<`/`>`
        # / JS/HTML special characters out of the attribute even if an
        # upstream proxy or misconfigured reverse proxy lets a crafted
        # header through.
        import re  # noqa: PLC0415
        ingress_path = re.sub(r"[^/A-Za-z0-9._-]", "", ingress_path)
        if not ingress_path:
            # Nothing left after sanitization — fall back to the default
            # `<base href="./">` rather than injecting an empty attribute.
            return web.Response(
                text=html,
                content_type="text/html",
                charset="utf-8",
            )
        # Ensure trailing slash for base href
        if not ingress_path.endswith("/"):
            ingress_path += "/"
        html = html.replace(
            '<base href="./">',
            f'<base href="{ingress_path}">',
        )

    return web.Response(
        text=html,
        content_type="text/html",
        charset="utf-8",
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    cfg = AppConfig.load()

    # AV.1 + Bug #19: run git auto-init FIRST, then settings. The
    # return value tells us whether a pre-existing repo was found
    # (False) or we created a fresh one (True). settings.init_settings
    # uses that signal on first boot to pick the auto-commit-on-save
    # default — pre-existing repo → off by default (Pat-with-git),
    # fresh-init → on by default (Pat-no-git). Sync call here is fine;
    # the event loop hasn't started yet.
    #
    # #191: if ``/config/esphome`` doesn't exist at all (truly-empty
    # first install — no HA ESPHome builder add-on, no pre-existing
    # Fleet data), ``init_repo`` used to return False which
    # ``init_settings`` read as "pre-existing repo" and logged
    # accordingly. Gate on directory existence so we pass ``None``
    # ("don't classify") instead — matches the semantics used by
    # tests / non-Supervisor harnesses.
    config_dir_path = Path(cfg.config_dir)
    if not config_dir_path.is_dir():
        fresh_repo = None
    else:
        try:
            from git_versioning import init_repo  # noqa: PLC0415
            fresh_repo = init_repo(config_dir_path)
        except Exception:
            logger.exception("git auto-init raised unexpectedly")
            fresh_repo = None  # "don't override the default"

    # SP.1/SP.2: load in-app settings (/data/settings.json) — created on
    # first boot after 1.6 upgrade and seeded from the current options.json
    # for any fields that have migrated. See ha-addon/server/settings.py.
    from settings import clear_supervisor_options_if_needed, init_settings  # noqa: PLC0415
    init_settings(fresh_repo_init=fresh_repo)
    # Bug #22 (1.6.1): the init_repo call above runs BEFORE
    # init_settings loads the persisted settings, so get_settings()
    # returns the default ``versioning_enabled="unset"`` and init_repo
    # silently skips. If the user had previously opted in
    # (settings.json carries ``versioning_enabled: "on"``) but the
    # .git/ directory is missing (container rebuild, fresh /config
    # mount on a restored backup, accidental delete), no rescue
    # happened until the user edited a file AND flipped the setting
    # to trigger the PATCH-handler's init_repo hook from #19. Re-try
    # the init now that the real setting is visible so a boot-time
    # recovery is automatic.
    try:
        from settings import get_settings  # noqa: PLC0415
        if get_settings().versioning_enabled == "on":
            from git_versioning import _is_git_repo, init_repo  # noqa: PLC0415
            if not _is_git_repo(Path(cfg.config_dir)):
                logger.info(
                    "Bug #22: versioning_enabled=on but %s has no .git/ — "
                    "running init_repo", cfg.config_dir,
                )
                init_repo(Path(cfg.config_dir))
    except Exception:
        logger.exception(
            "Bug #22: post-settings init_repo guard raised",
        )
    # Bug #9: after the settings have been safely imported, tell
    # Supervisor to drop its stale options cache so it stops spamming
    # "Option X does not exist in the schema" warnings on every read.
    # One-shot — a marker file under /data prevents re-POSTing on
    # subsequent boots.
    clear_supervisor_options_if_needed()

    # JH.1/JH.2: persistent job history DAO. One DAO per app; JobQueue
    # snapshots every terminal transition into it so the /ui/api/history
    # endpoint and per-device drawer survive queue coalescing + clears.
    # Init is lazy — the first record/query creates the DB on demand.
    # Deliberate: eager init on a test rig without /data writable would
    # crash startup where the real path would "just work".
    from job_history import JobHistoryDAO  # noqa: PLC0415
    job_history = JobHistoryDAO()

    queue = JobQueue(history=job_history)
    queue.load()

    # Workers persist until the operator deletes them from the Workers
    # tab — an add-on restart restores the list (offline until each
    # worker's next heartbeat) instead of starting empty.
    registry = WorkerRegistry(path="/data/workers.json")
    registry.load()

    # SP.8: device_poll_interval is now settings-driven. Pass the
    # current value as the initial interval; DevicePoller re-reads
    # live via get_settings() at each iteration, so drawer edits
    # take effect without a restart.
    from settings import get_settings as _get_settings_for_init  # noqa: PLC0415
    device_poller = DevicePoller(poll_interval=_get_settings_for_init().device_poll_interval)

    # FD.5: firmware upload needs a body budget larger than aiohttp's
    # 1 MB default. ESP32 `firmware.factory.bin` is 1-4 MB typically;
    # cyd-office-info hit this limit at 1.05 MB with the 1 MB default.
    # 16 MB is well above any plausible ESP firmware size.
    FIRMWARE_MAX_SIZE = 16 * 1024 * 1024
    # AU.2: ha_auth_middleware attaches request["ha_user"] for /ui/api/*
    # and (when require_ha_auth is on) 401s unauthenticated direct-port
    # calls. Kept separate from the worker-tier auth_middleware so the
    # two auth contracts don't collide — both run, each is a no-op for
    # paths outside its scope.
    from ha_auth import ha_auth_middleware  # noqa: PLC0415
    app = web.Application(
        client_max_size=FIRMWARE_MAX_SIZE,
        middlewares=[
            compression_middleware,
            security_headers_middleware,
            version_header_middleware,
            auth_middleware,
            ha_auth_middleware,
        ],
    )
    app["config"] = cfg
    app["queue"] = queue
    app["job_history"] = job_history
    app["registry"] = registry
    app["scanner_config_dir"] = cfg.config_dir
    app["device_poller"] = device_poller
    app["log_subscribers"] = {}
    # WL.2: per-worker log broker. Independent of the registry so log
    # transport state doesn't leak into worker liveness/config state.
    from worker_log_broker import WorkerLogBroker  # noqa: PLC0415
    app["worker_log_broker"] = WorkerLogBroker()

    # #109: diagnostics broker — holds pending thread-dump requests per
    # worker + a small cache of uploaded results keyed by request id
    # so the UI's polling endpoint has somewhere to find them.
    from diagnostics import DiagnosticsBroker  # noqa: PLC0415
    app["diagnostics_broker"] = DiagnosticsBroker()

    # TG.1: persistent worker-tag store. Keyed by worker identity (hostname,
    # falling back to client_id). First registration seeds from the worker's
    # WORKER_TAGS env; later registrations are server-side-wins so a UI tag
    # edit isn't clobbered by a worker restart.
    from worker_tags import WorkerTagStore  # noqa: PLC0415
    app["worker_tag_store"] = WorkerTagStore(path="/data/worker-tags.json")

    # DQ.2: persistent per-worker disk-quota override store. Same identity
    # scheme as worker_tag_store. None = inherit fleet default
    # (AppSettings.default_worker_disk_quota_bytes). The server pushes the
    # effective value to each worker on every heartbeat so a UI edit
    # propagates within one heartbeat tick without a worker restart.
    from worker_disk_quotas import WorkerDiskQuotaStore  # noqa: PLC0415
    app["worker_disk_quota_store"] = WorkerDiskQuotaStore(
        path="/data/worker-disk-quotas.json",
    )

    # TG.2: routing-rule store. JSON-backed list of "when device matches X,
    # worker must match Y" rules. The eligibility evaluator uses this list
    # at job-claim time; TG.4's REST endpoints (create/update/delete) and
    # TG.8's UI editor mutate it.
    from routing import RoutingRuleStore  # noqa: PLC0415
    app["routing_rule_store"] = RoutingRuleStore(path="/data/routing-rules.json")

    # Register routes
    app.router.add_routes(api_module.routes)
    app.router.add_routes(ui_api_module.routes)

    # Static file routes
    app.router.add_get("/", serve_index)
    app.router.add_get("/index.html", serve_index)
    if STATIC_DIR.is_dir():
        app.router.add_static("/static/", path=str(STATIC_DIR), name="static")
        # Serve Vite-built assets at /assets/ (referenced by base-relative URLs in index.html)
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.is_dir():
            app.router.add_static("/assets/", path=str(assets_dir), name="assets")

    # Mutable runtime state dict — set ONCE before app.start() so background
    # tasks can update its contents without triggering aiohttp's
    # DeprecationWarning ("Changing state of started or joined application").
    # All code that previously used app["key"] = value for these dynamic keys
    # should use app["_rt"]["key"] = value instead.
    app["_rt"] = {
        "esphome_detected_version": None,
        "esphome_available_versions": [],
        "esphome_versions_fetched_at": 0.0,
        "ha_entity_status": {},
        "ha_mac_set": set(),
        "ha_mac_to_device_id": {},
        "ha_name_to_device_id": {},
        "schedule_checker_started_at": None,
        "schedule_checker_tick_count": 0,
        "schedule_checker_last_tick": None,
        "schedule_checker_last_error": None,
    }

    # Startup/shutdown hooks
    async def on_startup(app: web.Application) -> None:
        logger.info("Starting Fleet for ESPHome")
        logger.info("Config dir: %s", cfg.config_dir)
        # SI (WORKITEMS-1.6.2): one-shot deployment-shape banner so
        # operators grep one line to confirm whether HA coupling is
        # active, instead of reading the absence/presence of nine
        # downstream "skipped — no SUPERVISOR_TOKEN" log lines.
        from helpers import ha_mode  # noqa: PLC0415
        mode = ha_mode()
        if mode == "standalone":
            logger.info(
                "Running in standalone mode (no HA Supervisor detected). "
                "HA-coupled features (auto-discovery, entity-driven device "
                "state, Supervisor-driven ESPHome version) are disabled; "
                "the rest of the server runs unchanged. See "
                "dev-plans/HA-COUPLING-AUDIT.md for the full matrix."
            )
        else:
            logger.info("Running as HA add-on (Supervisor detected)")
        from settings import get_settings as _get_settings_startup  # noqa: PLC0415
        logger.info("Token configured: %s", bool(_get_settings_startup().server_token))

        # HI.8: auto-install the bundled HA custom integration into
        # /config/custom_components/esphome_fleet on every boot.
        # Failures are logged and swallowed so the add-on keeps running.
        try:
            from integration_installer import install_integration  # noqa: PLC0415
            install_integration()
        except Exception:
            logger.exception("HA integration auto-install raised unexpectedly")

        # AV.1: git auto-init already ran synchronously in create_app
        # (before settings, so the fresh-vs-existing signal can drive
        # the auto-commit default — see Bug #19). No work to do here.

        # Use the locally installed ESPHome package version as the initial
        # active version.  The pypi_version_refresher background task will
        # contact the HA Supervisor API shortly after startup and update the
        # version if it detects a different version in the ESPHome add-on.
        # Doing this at startup (instead of blocking on the Supervisor API
        # here) avoids a 10–15 s startup delay when the Supervisor is slow
        # or the hassio_api permission is not yet granted, which previously
        # caused the web server to refuse connections during that window.
        from scanner import (  # noqa: PLC0415
            scan_configs, build_name_to_target_map,
            set_esphome_version, _get_installed_esphome_version,
        )

        # `_get_installed_esphome_version()` returns the string "installing"
        # or "unknown" as diagnostic sentinels when no ESPHome is bundled.
        # Those aren't real versions — don't cache them as the selected
        # version (would pollute UI payloads and compile-job stamps).
        # Leave `_selected_esphome_version` unset; the install flow below
        # will set it once it resolves a real version.
        selected = _get_installed_esphome_version()
        if selected not in ("installing", "unknown"):
            set_esphome_version(selected)
            logger.info(
                "Active ESPHome version: %s (background task will refine from HA Supervisor)",
                selected,
            )
        else:
            logger.info(
                "No bundled ESPHome; version will be resolved from HA Supervisor "
                "or PyPI fallback (bug #30)"
            )

        app["esphome_install_task"] = asyncio.create_task(_install_esphome_initial(app))

        # Update device poller with known targets. Runs in executor —
        # see reseed_device_poller_from_config for the rationale (full
        # ESPHome validation is CPU-bound and blocks the event loop).
        startup_loop = asyncio.get_running_loop()
        targets = scan_configs(cfg.config_dir)
        name_map, enc_keys, addr_overrides, addr_sources = await startup_loop.run_in_executor(
            None, build_name_to_target_map, cfg.config_dir, targets,
        )
        device_poller.update_compile_targets(targets, name_map, enc_keys, addr_overrides, addr_sources)

        # Start device poller
        await device_poller.start(app)

        # HI.7: advertise ourselves on mDNS so the HA custom
        # integration's zeroconf config-flow discovers us.
        try:
            from zeroconf.asyncio import AsyncZeroconf  # noqa: PLC0415
            from mdns_advertiser import FleetAdvertiser  # noqa: PLC0415
            app["_rt"]["mdns_zeroconf"] = AsyncZeroconf()
            advertiser = FleetAdvertiser(app["_rt"]["mdns_zeroconf"], cfg.port)
            await advertiser.start()
            app["_rt"]["mdns_advertiser"] = advertiser
        except Exception:
            logger.exception("Failed to start mDNS advertiser (HI.7)")

        # #26: register ourselves with Supervisor's /discovery API so the
        # custom integration can auto-configure without a URL prompt.
        # AU.7: the payload now includes `token` so the integration's
        # coordinator can authenticate against `/ui/api/*` without asking
        # the user to paste credentials.
        try:
            from supervisor_discovery import register_discovery  # noqa: PLC0415
            from settings import get_settings as _get_s  # noqa: PLC0415
            app["_rt"]["supervisor_discovery_uuid"] = await register_discovery(
                cfg.port, token=_get_s().server_token,
            )
        except Exception:
            logger.debug("Supervisor discovery registration raised", exc_info=True)

        # Start background tasks
        app["timeout_checker_task"] = asyncio.create_task(timeout_checker(app))
        app["config_scanner_task"] = asyncio.create_task(config_scanner(app))
        app["pypi_version_refresher_task"] = asyncio.create_task(pypi_version_refresher(app))
        app["ha_entity_poller_task"] = asyncio.create_task(ha_entity_poller(app))
        # JH.3: nightly job-history retention task. Reads the Settings
        # value live each tick so drawer edits take effect next run.
        app["job_history_retention_task"] = asyncio.create_task(job_history_retention(app))
        # Bug #38 + #198: firmware retention + disk-budget enforcer.
        # Complements reconcile_orphans at startup — this one runs
        # periodically while the server is up so binaries get evicted
        # by age (#198, default 2 days) and as a hard ceiling on
        # ``firmware_cache_max_gb`` (#38).
        app["firmware_retention_task"] = asyncio.create_task(firmware_retention_enforcer(app))

        # TG.3: defensive 30-s routing-rule watchdog. Backstop against
        # missed explicit triggers (registry mutations, rule CRUD,
        # enqueue) — when those fire correctly this sees zero state
        # changes and stays quiet.
        from routing_eligibility import routing_watchdog  # noqa: PLC0415
        app["routing_watchdog_task"] = asyncio.create_task(routing_watchdog(app))

        # #87: APScheduler replaces the DIY schedule_checker
        import scheduler as scheduler_module  # noqa: PLC0415
        scheduler_module.start(app)

        # Start local worker if client code is bundled
        local_worker_script = Path("/app/client/client.py")
        if local_worker_script.exists():
            import subprocess as sp  # noqa: PLC0415
            # #99: fresh installs default to 1 slot (active). Previously
            # defaulted to 0 (paused) which was a poor out-of-the-box
            # experience — new users saw "local-worker: 0 slots" and
            # had to discover the +/- buttons before any compile would
            # run. If the user has explicitly configured a slot count
            # via the UI, the persisted file wins; we only use the
            # default when the file is absent.
            local_slots_file = Path("/data/local_worker_slots")
            local_slots = "1"
            try:
                if local_slots_file.exists():
                    local_slots = local_slots_file.read_text().strip() or "1"
            except Exception:
                pass
            # SP.8: local-worker is a subprocess started once at add-on
            # boot. Its SERVER_TOKEN is captured at spawn time; if the
            # user later rotates the token via the Settings drawer, the
            # local worker keeps using the old token until the add-on
            # restarts (documented behavior; remote workers have the
            # same property). Read the current value fresh at spawn.
            from settings import get_settings as _get_s_lw  # noqa: PLC0415
            local_env = {
                **os.environ,
                "SERVER_URL": f"http://127.0.0.1:{cfg.port}",
                "SERVER_TOKEN": _get_s_lw().server_token,
                "MAX_PARALLEL_JOBS": local_slots,
                "ESPHOME_VERSIONS_DIR": "/data/esphome-versions",
                "HOSTNAME": "local-worker",
            }
            # Capture the embedded worker's stderr to a rotating-ish
            # tail file under /data so next-time-this-breaks is
            # diagnosable. #193: previously DEVNULL'd both streams, so
            # a ModuleNotFoundError at import turned the worker into a
            # silent zombie and the server logged "Started local worker"
            # as if everything was fine. stdout stays DEVNULL — the
            # worker already logs to its own logger via SERVER_URL.
            local_worker_log = Path("/data/local-worker.stderr.log")
            stderr_fh: int | IO[bytes]
            try:
                local_worker_log.parent.mkdir(parents=True, exist_ok=True)
                stderr_fh = local_worker_log.open("ab")
            except OSError:
                logger.warning("Couldn't open %s for local-worker stderr; falling back to DEVNULL", local_worker_log)
                stderr_fh = sp.DEVNULL
            proc = sp.Popen(
                [sys.executable, str(local_worker_script)],
                env=local_env,
                stdout=sp.DEVNULL,
                stderr=stderr_fh,
            )
            app["local_worker_proc"] = proc
            logger.info("Started local worker (PID %d, %s slots); stderr → %s", proc.pid, local_slots, local_worker_log)

    async def on_shutdown(app: web.Application) -> None:
        logger.info("Shutting down Fleet for ESPHome")

        # Stop local worker
        proc = app.get("local_worker_proc")
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            logger.info("Local worker stopped")

        for task_name in ("timeout_checker_task", "config_scanner_task", "pypi_version_refresher_task", "ha_entity_poller_task", "esphome_install_task", "job_history_retention_task", "firmware_retention_task"):
            task = app.get(task_name)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Flush the latest heartbeat timestamps so "offline for" is
        # accurate after the restart (heartbeat saves are throttled).
        registry.save()

        # #87: stop APScheduler
        import scheduler as scheduler_module  # noqa: PLC0415
        scheduler_module.stop()

        # #26: deregister from Supervisor discovery so a fresh install
        # after a reinstall gets a clean discovery flow.
        discovery_uuid = app["_rt"].get("supervisor_discovery_uuid")
        if discovery_uuid:
            try:
                from supervisor_discovery import unregister_discovery  # noqa: PLC0415
                await unregister_discovery(discovery_uuid)
            except Exception:
                logger.debug("Supervisor discovery unregister failed", exc_info=True)

        # HI.7: tear down mDNS advertiser.
        advertiser = app["_rt"].get("mdns_advertiser")
        if advertiser is not None:
            try:
                await advertiser.stop()
            except Exception:
                logger.debug("mDNS advertiser stop failed", exc_info=True)
        zc = app["_rt"].get("mdns_zeroconf")
        if zc is not None:
            try:
                await zc.async_close()
            except Exception:
                logger.debug("mDNS Zeroconf close failed", exc_info=True)

        await device_poller.stop()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def bind_conflict_message(port: int) -> str:
    """Explain an ``EADDRINUSE`` on *port* in terms a user can act on (#114).

    The bind failure is raised out of ``web.run_app`` *after* aiohttp has
    already run the ``on_shutdown`` handlers, so the log ends with a
    tidy-looking shutdown sequence (scheduler stopped, mDNS unregistered,
    poller stopped) and only then the traceback. Reporters read that as
    "something told the add-on to stop" and go looking in the wrong place
    — #114 took four rounds of back-and-forth to land on the real cause.

    The port is not user-changeable under Supervisor: ``ingress_port`` in
    ``config.yaml`` is read once at add-on install and the server has to
    bind exactly it or Ingress breaks. The port field in the add-on's
    Configuration tab only relabels the host-side mapping, which does
    nothing here because the add-on runs with ``host_network: true``.
    Saying so up front stops the next reporter from "fixing" it there.
    """
    return (
        f"Cannot bind to port {port} — address already in use. Fleet for ESPHome "
        f"runs on the host network, so any other process holding {port} blocks it. "
        "The most common culprit is the OpenThread Border Router add-on, which is "
        "also host-networked and binds this port "
        "(`ha addons stop core_openthread_border_router` to check). "
        "Changing the port in this add-on's Configuration tab will NOT help — "
        "Home Assistant Ingress pins the port the server binds inside the "
        "container. Free the port on the host instead. "
        "Note: the shutdown messages logged just above are this process cleaning "
        "up after the failed bind, not a stop request."
    )


def run_server(app: web.Application, port: int) -> None:
    """Serve *app* on *port*, turning a port clash into a readable error (#114)."""
    try:
        web.run_app(app, host="0.0.0.0", port=port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            logger.critical("%s", bind_conflict_message(port))
            sys.exit(1)
        raise


if __name__ == "__main__":
    cfg = AppConfig.load()
    app = create_app()
    run_server(app, cfg.port)
