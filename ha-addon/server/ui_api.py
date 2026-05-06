"""Web UI API handlers (/ui/api/*) — no auth (HA ingress handles it)."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import OrderedDict
from pathlib import Path

import aiohttp
from aiohttp import web

from app_config import AppConfig
from helpers import safe_resolve, json_error
from device_poller import Device
from job_queue import JobState
from scanner import (
    create_stub_yaml,
    duplicate_device,
    get_archived_device_metadata,
    get_device_metadata,
    get_esphome_version,
    read_device_meta,
    scan_configs,
    set_esphome_version,
    write_device_meta,
)

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _parse_device_compile_epoch(compilation_time: str | None) -> int | None:
    """Parse the ESPHome ``compilation_time`` string into epoch seconds.

    Bug #13 / #102: serves as the device-firmware fallback for the
    Devices-tab "Last compiled" column when the SQLite job history has
    nothing for a target. ``aioesphomeapi`` reports the build time as
    ``"2026-04-23 06:13:56 -0700"`` (ISO-with-offset); the dev.18 parser
    hard-coded the older ``"%b %d %Y, %H:%M:%S"`` form ("Mar 29 2026,
    17:00:00") which never matched in production, so the fallback
    silently returned None for every device. We try the modern format
    first, then the older comma-separated form for back-compat. Tz-aware
    parses produce a UTC-correct epoch; tz-naive parses fall back to
    server-local time as the closest defensible interpretation (we don't
    know the build host's timezone).
    """
    if not compilation_time:
        return None
    from datetime import datetime  # noqa: PLC0415
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%b %d %Y, %H:%M:%S"):
        try:
            return int(datetime.strptime(compilation_time, fmt).timestamp())
        except (ValueError, OSError):
            continue
    return None


def _broadcast_ws(event_type: str, **payload: object) -> None:
    """Fire a state-change event on the WebSocket bus (#41).

    Thin wrapper around :func:`event_bus.broadcast` so call sites don't
    have to import/except. Silent no-op on any failure — the 30 s HA
    coordinator poll still catches the change.
    """
    try:
        from event_bus import broadcast  # noqa: PLC0415
        broadcast(event_type, **payload)
    except Exception:
        logger.debug("event_bus broadcast failed", exc_info=True)


def _who(request: web.Request) -> str:
    """AU.4: attribution suffix for mutation log lines.

    Returns ``" by <name>"`` when the ha_auth_middleware resolved an HA
    user on this request, empty string otherwise. Used to tack a "by
    stefan" onto log lines like ``Pinned foo.yaml to 2026.4.0`` so
    operators can trace who enqueued what.
    """
    user = request.get("ha_user")
    if not user:
        return ""
    name = user.get("name")
    return f" by {name}" if name else ""

# Module-level cache: populated once per server lifetime (components don't
# change until ESPHome is upgraded, which restarts the add-on).
_esphome_components_cache: list[str] | None = None


def _cfg(request: web.Request) -> AppConfig:
    return request.app["config"]


async def _ensure_pinned_esphome_bin(pin: str) -> str:
    """Lazy-install the pinned ESPHome version into ``/data/esphome-versions``
    and return the path to its ``esphome`` binary.

    Used by the validate and rendered-config endpoints, both of which run
    a one-off subprocess outside the worker bundle path. Wraps the
    blocking ``VersionManager.ensure_version`` call so the event loop
    isn't held for the install duration.
    """
    import asyncio as _asyncio  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415
    if "/app/client" not in _sys.path:
        _sys.path.insert(0, "/app/client")
    from version_manager import VersionManager  # noqa: PLC0415
    vm = VersionManager(
        versions_base=_Path("/data/esphome-versions"),
        max_versions=5,
    )
    return await _asyncio.get_running_loop().run_in_executor(
        None, vm.ensure_version, pin,
    )


@routes.get("/ui/api/_debug/scheduler")
async def debug_scheduler(request: web.Request) -> web.Response:
    """Diagnostic endpoint — reports on the APScheduler state (#87)."""
    import scheduler as scheduler_module  # noqa: PLC0415
    return web.json_response({
        "engine": "apscheduler",
        "jobs": scheduler_module.get_jobs_info(),
    })


@routes.get("/ui/api/schedule-history")
async def get_schedule_history(request: web.Request) -> web.Response:
    """Return the schedule fire history for all targets (#81)."""
    import schedule_history  # noqa: PLC0415
    all_history = schedule_history.get_all()
    result: dict[str, list[dict]] = {}
    for target, entries in all_history.items():
        result[target] = [
            {"fired_at": fired_at.isoformat(), "job_id": job_id, "outcome": outcome}
            for fired_at, job_id, outcome in entries
        ]
    return web.json_response(result)


@routes.get("/ui/api/esphome-schema")
async def get_esphome_schema(request: web.Request) -> web.Response:
    """Return ESPHome component names for editor autocomplete.

    Walks the esphome/components directory of the locally installed package so
    the list reflects exactly what is available, rather than a hardcoded subset.
    The result is cached in memory for the lifetime of the server process.
    """
    global _esphome_components_cache
    if _esphome_components_cache is None:
        try:
            from pathlib import Path as _Path  # noqa: PLC0415
            import scanner as _scanner  # noqa: PLC0415

            # SE.5: walk the venv's components directory directly instead of
            # importing esphome.loader. This sidesteps the chicken-and-egg
            # problem where the venv is on sys.path but Python has already
            # cached a half-resolved `esphome` module object from an earlier
            # failed import. When the venv isn't ready yet, fall through to
            # the old import-based path (covers pre-SE.1 bundled package +
            # the test harness).
            comps_path = None
            if _scanner._esphome_ready.is_set() and _scanner._server_esphome_venv:
                import sys as _sys  # noqa: PLC0415
                candidate = (
                    _scanner._server_esphome_venv / "lib"
                    / f"python{_sys.version_info.major}.{_sys.version_info.minor}"
                    / "site-packages" / "esphome" / "components"
                )
                if candidate.is_dir():
                    comps_path = candidate
            if comps_path is None:
                try:
                    import esphome.loader as _loader  # noqa: PLC0415
                    comps_path = _Path(_loader.__file__).parent / "components"
                except ImportError:
                    # Install still in flight and no bundled package —
                    # return an empty list; autocomplete briefly off.
                    logger.info(
                        "ESPHome still installing — components list empty until venv is ready"
                    )
                    return web.json_response({"components": []})

            names = sorted({
                p.stem
                for p in comps_path.iterdir()
                if (p.is_dir() and (p / "__init__.py").exists())
                or (p.suffix == ".py" and p.stem != "__init__")
            })
            # Ensure well-known root keys are always present even if the
            # directory walk misses them (e.g. "esphome" core block).
            for core_key in ("esphome", "substitutions", "packages", "external_components"):
                if core_key not in names:
                    names.append(core_key)
                    names.sort()
            _esphome_components_cache = names
            logger.debug("ESPHome component list cached: %d components", len(names))
        except Exception:
            logger.debug("Could not enumerate ESPHome components", exc_info=True)
            _esphome_components_cache = []
    return web.json_response({"components": _esphome_components_cache})


# ---------------------------------------------------------------------------
# AV.3 / AV.4 — file history + diff
# ---------------------------------------------------------------------------

@routes.get("/ui/api/files/{filename}/history")
async def get_file_history(request: web.Request) -> web.Response:
    """AV.3: per-file git history (newest first), paginated.

    Query params:
      - ``limit`` (default 50, max 500) — page size
      - ``offset`` (default 0) — how many commits to skip

    Returns ``[{hash, short_hash, date, author_name, author_email,
    message, lines_added, lines_removed}]``. Empty list if the file has
    no commit history yet (and 200 either way — callers render a
    friendly "no history yet" rather than treating empty as an error).
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None:
        return json_error("Invalid filename", 400)

    try:
        limit = min(max(int(request.rel_url.query.get("limit", "50")), 1), 500)
    except ValueError:
        return json_error("limit must be an integer", 400)
    try:
        offset = max(int(request.rel_url.query.get("offset", "0")), 0)
    except ValueError:
        return json_error("offset must be an integer", 400)

    from git_versioning import file_history  # noqa: PLC0415
    entries = file_history(Path(cfg.config_dir), filename, limit=limit, offset=offset)
    # #211: enrich entries with firmware-availability info so the
    # History panel can render a per-row Download chip when a stored
    # binary still matches that commit's config_hash. The map is
    # naturally sparse — most commits have no matching firmware.
    history_dao = request.app.get("job_history")
    if history_dao is not None and entries:
        hashes = [str(e.get("hash") or "") for e in entries]
        try:
            firmware_by_hash = history_dao.latest_firmware_by_hash(filename, hashes)
        except Exception:
            firmware_by_hash = {}
        for e in entries:
            match = firmware_by_hash.get(str(e.get("hash") or ""))
            if match:
                e["firmware_job_id"] = match["job_id"]
                e["firmware_variants"] = match["firmware_variants"]
    return web.json_response(entries)


@routes.get("/ui/api/files/{filename}/content-at")
async def get_file_content_at(request: web.Request) -> web.Response:
    """Bug #10: return the content of a file at a specific commit (or
    working tree if no hash given). Feeds the side-by-side diff view.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None:
        return json_error("Invalid filename", 400)

    hash_arg = request.rel_url.query.get("hash", "").strip() or None

    from git_versioning import file_content_at  # noqa: PLC0415
    content = file_content_at(Path(cfg.config_dir), filename, hash_arg)
    if content is None:
        return json_error("Could not read file at that version", 400)
    return web.json_response({"content": content})


@routes.get("/ui/api/files/{filename}/status")
async def get_file_status(request: web.Request) -> web.Response:
    """AV.6: per-file dirtiness + HEAD info for the history-panel banner.

    Returns ``{has_uncommitted_changes, head_hash, head_short_hash}``.
    Used by the panel to show the "You have uncommitted changes" banner
    without chaining a separate status call.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None:
        return json_error("Invalid filename", 400)

    from git_versioning import file_status  # noqa: PLC0415
    return web.json_response(file_status(Path(cfg.config_dir), filename))


@routes.post("/ui/api/files/{filename}/rollback")
async def post_file_rollback(request: web.Request) -> web.Response:
    """AV.5: restore a file to a historical commit's content.

    Body: ``{hash: "<sha>"}``. Returns ``{content, committed, hash,
    short_hash}`` — ``content`` is the restored file text, ``committed``
    tells the UI whether a new revert commit was created (happens when
    ``settings.auto_commit_on_save`` is on).
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None:
        return json_error("Invalid filename", 400)
    if not path.exists():
        return json_error("Target not found", 404)

    try:
        body = await request.json()
    except Exception:
        return json_error("Request body must be JSON", 400)

    target_hash = (body.get("hash") or "").strip() if isinstance(body, dict) else ""
    if not target_hash:
        return json_error("hash is required", 400)

    from git_versioning import rollback_file  # noqa: PLC0415
    result = rollback_file(Path(cfg.config_dir), filename, target_hash)
    if not result.get("content"):
        return json_error("Rollback failed (invalid hash or git error)", 400)

    # Invalidate the scanner config cache so a subsequent /content read
    # sees the rolled-back file.
    from scanner import _config_cache  # noqa: PLC0415
    _config_cache.pop(filename, None)

    logger.info(
        "Rolled back %s to %s%s%s",
        filename,
        target_hash[:7],
        " (committed)" if result.get("committed") else " (working tree only)",
        _who(request),
    )
    _broadcast_ws("targets_changed")
    return web.json_response(result)


@routes.post("/ui/api/files/{filename}/commit")
async def post_file_commit(request: web.Request) -> web.Response:
    """AV.11: explicitly commit any pending changes to a single file.

    Body: ``{message?: str}``. Returns ``{committed, hash, short_hash,
    message}``. ``committed: false`` with ``null`` hash means there was
    nothing to commit — not an error.

    Always runs regardless of ``settings.auto_commit_on_save`` — this
    is the manual-commit escape valve.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None:
        return json_error("Invalid filename", 400)

    body: dict = {}
    if request.can_read_body:
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
        except Exception:
            return json_error("Request body must be JSON", 400)

    message = body.get("message")
    if message is not None and not isinstance(message, str):
        return json_error("message must be a string", 400)

    from git_versioning import commit_file_now  # noqa: PLC0415
    result = commit_file_now(Path(cfg.config_dir), filename, message=message)
    logger.info(
        "Manual commit for %s: %s%s",
        filename,
        result.get("short_hash") or "(no-op)",
        _who(request),
    )
    return web.json_response(result)


@routes.get("/ui/api/files/{filename}/diff")
async def get_file_diff(request: web.Request) -> web.Response:
    """AV.4: unified diff for a file between two commits (or against HEAD).

    Query params:
      - ``from`` — commit hash (optional; omit to diff working tree vs HEAD)
      - ``to`` — commit hash (optional; omit to diff *from* against HEAD)

    Returns ``{"diff": "<unified diff text>"}``. Empty string when the
    two versions are identical or the file has no history.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None:
        return json_error("Invalid filename", 400)

    from_hash = request.rel_url.query.get("from", "").strip() or None
    to_hash = request.rel_url.query.get("to", "").strip() or None

    from git_versioning import file_diff  # noqa: PLC0415
    diff = file_diff(Path(cfg.config_dir), filename, from_hash=from_hash, to_hash=to_hash)
    return web.json_response({"diff": diff})


@routes.get("/ui/api/settings")
async def get_settings_handler(request: web.Request) -> web.Response:
    """SP.3: return the current in-app Settings blob.

    Editable via :func:`patch_settings_handler`. Distinct from the
    Supervisor-owned ``options.json`` (see ``app_config.py``).
    """
    from settings import settings_as_dict  # noqa: PLC0415
    return web.json_response(settings_as_dict())


def _versioning_just_enabled(previous: str | None, partial: dict) -> bool:
    """Bug #19 (1.6.1): pure-function transition detector used by
    :func:`patch_settings_handler` to decide whether to fire
    :func:`git_versioning.init_repo` after a PATCH. Lifted out of the
    handler so it's testable without the aiohttp request harness.

    Returns True only when the partial update explicitly sets
    ``versioning_enabled`` to ``"on"`` AND the previous value was
    something else (``"unset"`` first boot, ``"off"`` deliberate opt
    out). No-ops when the key isn't in the partial at all — that's
    important because other PATCH calls mustn't re-trigger init.
    """
    if partial.get("versioning_enabled") != "on":
        return False
    return previous != "on"


@routes.patch("/ui/api/settings")
async def patch_settings_handler(request: web.Request) -> web.Response:
    """SP.3: partial update of in-app Settings.

    Body is a JSON object ``{key: value, ...}`` — any subset of the
    known fields. Unknown keys or out-of-range values return 400 with
    the offending field name so the UI can surface the error.
    """
    from settings import SettingsValidationError, get_settings, settings_as_dict, update_settings  # noqa: PLC0415

    try:
        partial = await request.json()
    except Exception:
        return json_error("Request body must be JSON", status=400)

    if not isinstance(partial, dict):
        return json_error("Request body must be a JSON object", status=400)

    # Bug #19 (1.6.1): capture the pre-write value so we can detect the
    # versioning_enabled "unset|off → on" transition and kick off a
    # one-shot ``git init`` in the config directory. Without this the
    # user flips the toggle on, every subsequent save lands on disk
    # but silently bypasses commit_file (no .git/), and the History
    # drawer reads empty until the next add-on restart.
    previous_versioning = get_settings().versioning_enabled

    try:
        await update_settings(partial)
    except SettingsValidationError as exc:
        return web.json_response(
            {"error": str(exc), "field": exc.field},
            status=400,
        )

    # Bug #19: post-swap hook. Runs outside the settings lock so a
    # slow ``git init`` doesn't stall concurrent settings reads.
    if _versioning_just_enabled(previous_versioning, partial):
        try:
            from git_versioning import init_repo  # noqa: PLC0415
            cfg = _cfg(request)
            loop = asyncio.get_running_loop()
            created = await loop.run_in_executor(None, init_repo, Path(cfg.config_dir))
            if created:
                logger.info(
                    "Bug #19: git repo initialised at %s after post-boot "
                    "versioning_enabled flip (was %r → on)",
                    cfg.config_dir, previous_versioning,
                )
        except Exception:
            logger.exception(
                "Bug #19: init_repo failed after versioning flip; history "
                "will stay empty until the add-on is restarted. See "
                "dev-plans/WORKITEMS-1.6.1.md bug #19 for context.",
            )

    logger.info("Settings updated%s: %s", _who(request), ", ".join(sorted(partial.keys())))
    return web.json_response(settings_as_dict())


@routes.get("/ui/api/server-info")
async def get_server_info(request: web.Request) -> web.Response:
    """Return server configuration needed by the UI (token, port, versions)."""
    import socket  # noqa: PLC0415
    from api import _get_server_client_version  # noqa: PLC0415
    cfg = _cfg(request)
    addon_version = _get_server_client_version()

    # Collect all addresses the server is reachable on.
    # Start with hostname, then enumerate all non-loopback IPv4 addresses.
    addrs: list[str] = []
    try:
        hostname = socket.gethostname()
        addrs.append(hostname)
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in addrs and not ip.startswith("127."):
                addrs.append(ip)
    except Exception:
        pass
    # Use a UDP connect trick to find the primary outbound IP (most useful for workers)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        primary_ip = s.getsockname()[0]
        s.close()
        if primary_ip not in addrs:
            addrs.insert(0, primary_ip)
    except Exception:
        pass

    # Backwards-compat: server_ip is the first IP address found (or None)
    ip_addrs = [a for a in addrs if a.replace(".", "").isdigit()]
    server_ip = ip_addrs[0] if ip_addrs else (addrs[0] if addrs else None)

    from constants import MIN_IMAGE_VERSION  # noqa: PLC0415
    # SE.8: surface the server's ESPHome install status so the UI can
    # render a top-of-app banner during the first-boot install window.
    import scanner as _scanner  # noqa: PLC0415
    if _scanner._esphome_ready.is_set():
        esphome_install_status = "ready"
    elif _scanner._esphome_install_failed:
        esphome_install_status = "failed"
    else:
        esphome_install_status = "installing"
    from settings import get_settings as _gs  # noqa: PLC0415
    settings = _gs()
    return web.json_response({
        "token": settings.server_token,
        "port": cfg.port,
        "server_ip": server_ip,
        "server_addresses": addrs,
        "server_client_version": addon_version,
        "addon_version": addon_version,
        "min_image_version": MIN_IMAGE_VERSION,
        # SE.8: ESPHome install lifecycle fields for the UI banner.
        "esphome_install_status": esphome_install_status,
        "esphome_server_version": _scanner.get_esphome_version(),
        # DQ.5: ConnectWorkerModal needs the fleet default to label its
        # "Use fleet default (X GiB)" radio without a separate fetch.
        "default_worker_disk_quota_bytes": settings.default_worker_disk_quota_bytes,
    })


def _normalize_for_ha(name: str) -> str:
    """Normalize a name to match HA entity_id conventions (underscores, lowercase).

    HA entity IDs strip special characters like &, ', etc. and collapse
    multiple underscores.
    """
    import re  # already imported at module level
    normalized = name.replace("-", "_").replace(" ", "_").lower()
    normalized = re.sub(r"[^a-z0-9_]", "", normalized)  # strip non-alphanumeric
    normalized = re.sub(r"_+", "_", normalized)  # collapse multiple underscores
    return normalized.strip("_")


def _ha_status_for_target(
    ha_entity_status: dict[str, dict],
    target: str,
    meta: dict,
    device_mac: str | None = None,
    ha_mac_set: set[str] | None = None,
    ha_mac_to_device_id: dict[str, str] | None = None,
    ha_name_to_device_id: dict[str, str] | None = None,
) -> tuple[bool, bool | None, str | None]:
    """Return (ha_configured, ha_connected, ha_device_id) for a compile target.

    Matching priority:
    1. MAC address (most reliable — HA identifies ESPHome devices by MAC)
    2. Direct name lookup (friendly_name, esphome.name, filename)
    3. Prefix match against entity locals

    Returns (False, None, None) when no match is found.

    #35: ha_device_id is resolved via MAC match (most reliable).
    #41: falls back to the ha_name_to_device_id map (built from HA entity IDs)
    when no MAC is available — e.g. offline devices that the local mDNS/API
    poller can't reach right now.
    """
    # 1. MAC address match (authoritative — doesn't depend on naming)
    #    HA connections store MACs as "aa:bb:cc:dd:ee:ff" (lowercase with colons).
    #    Device poller MACs from aioesphomeapi are "AA:BB:CC:DD:EE:FF" (uppercase).
    ha_device_id: str | None = None
    if device_mac and ha_mac_set:
        mac_lower = device_mac.lower()
        mac_confirmed = mac_lower in ha_mac_set
        if mac_confirmed and ha_mac_to_device_id:
            ha_device_id = ha_mac_to_device_id.get(mac_lower)
    else:
        mac_confirmed = False

    if not ha_entity_status and not mac_confirmed:
        return False, None, ha_device_id

    # 2. Name matching for connectivity state
    candidates: list[str] = []
    friendly = meta.get("friendly_name")
    if friendly:
        candidates.append(_normalize_for_ha(friendly))
    raw_name = meta.get("device_name_raw")
    if raw_name:
        candidates.append(_normalize_for_ha(raw_name))
    candidates.append(_normalize_for_ha(target.replace(".yaml", "")))

    # Helper: fall back to name-based device_id lookup when we don't have
    # one from the MAC path. Offline devices commonly land here because the
    # local poller has no MAC for them right now.
    def _resolve_id(match_name: str) -> str | None:
        if ha_device_id:
            return ha_device_id
        if ha_name_to_device_id:
            return ha_name_to_device_id.get(match_name)
        return None

    # Direct lookup
    for norm_name in candidates:
        entry = ha_entity_status.get(norm_name)
        if entry:
            return True, entry.get("connected"), _resolve_id(norm_name)

    # Prefix match
    for norm_name in candidates:
        prefix = norm_name + "_"
        for key, entry in ha_entity_status.items():
            if key.startswith(prefix) or key == norm_name:
                return True, entry.get("connected"), _resolve_id(key)

    # 3. MAC fragment match — some devices register with HA using internal names
    #    that include MAC fragments (e.g. screek_humen_sensor_1u_c76926 contains
    #    the last 3 bytes of MAC 84:FC:E6:C7:69:26 as "c76926").
    if device_mac:
        mac_suffix = device_mac.upper().replace(":", "")[-6:].lower()  # last 3 bytes
        if mac_suffix and len(mac_suffix) == 6:
            for key, entry in ha_entity_status.items():
                if mac_suffix in key:
                    return True, entry.get("connected"), _resolve_id(key)

    # 4. If MAC confirmed via HA device identifiers but name didn't match
    if mac_confirmed:
        return True, None, ha_device_id

    return False, None, ha_device_id


@routes.get("/ui/api/targets")
async def get_targets(request: web.Request) -> web.Response:
    """List discovered YAML targets with device status."""
    cfg = _cfg(request)
    device_poller = request.app.get("device_poller")
    server_version = get_esphome_version()
    ha_entity_status: dict[str, dict] = request.app["_rt"].get("ha_entity_status", {})
    ha_mac_set: set[str] = request.app["_rt"].get("ha_mac_set", set())
    ha_mac_to_device_id: dict[str, str] = request.app["_rt"].get("ha_mac_to_device_id", {})
    ha_name_to_device_id: dict[str, str] = request.app["_rt"].get("ha_name_to_device_id", {})

    targets = scan_configs(cfg.config_dir)

    # Bug #16: per-target uncommitted-changes flag. One bulk
    # `git status --porcelain` for the whole repo, then O(1) lookup
    # per target in the loop below. Empty set when the dir isn't a
    # git repo — the flag defaults to False in that case.
    from git_versioning import changed_paths_between, dirty_paths, get_head  # noqa: PLC0415
    dirty_set = dirty_paths(Path(cfg.config_dir))

    # Bug #32: per-target "did the YAML change since we last flashed it?"
    # flag, used by the UI to recolor the Upgrade button. Built in three
    # steps so we stay O(targets + unique_hashes) git invocations:
    #   1. Walk the queue's finished jobs, newest finish first, to pick
    #      each target's most recent successful OTA config_hash.
    #   2. For every unique (last_flashed_hash != HEAD) combo, run one
    #      `git diff --name-only <hash> HEAD` and cache the result set.
    #   3. A target is "drifted" if its YAML appears in that hash's
    #      changed-files set. Same hash as HEAD → no drift. No flash
    #      on record, or no git repo → null (UI falls back to the older
    #      mtime-based ``config_modified`` signal).
    head_hash = get_head(Path(cfg.config_dir))
    queue = request.app.get("queue")
    last_flashed_by_target: dict[str, str] = {}
    if queue is not None:
        # Sort oldest first so newer jobs overwrite older entries in the dict.
        try:
            jobs_for_flash = sorted(
                queue.get_all(),
                key=lambda j: (j.finished_at.timestamp() if j.finished_at else 0.0),
            )
        except Exception:
            jobs_for_flash = []
        for job in jobs_for_flash:
            if (
                getattr(job, "config_hash", None)
                and getattr(job, "ota_result", None) == "success"
                and not getattr(job, "validate_only", False)
                and not getattr(job, "download_only", False)
            ):
                last_flashed_by_target[job.target] = job.config_hash
    drift_cache: dict[str, set[str]] = {}
    if head_hash:
        for h in set(last_flashed_by_target.values()):
            if h and h != head_hash:
                drift_cache[h] = changed_paths_between(Path(cfg.config_dir), h, head_hash)

    # JH.6: per-target "last compiled" rollup. One SQL query that groups
    # by target so the Devices tab's optional "Last compiled" column can
    # show the most recent outcome without an N+1 call to /ui/api/history.
    # None when the DAO is unavailable or the target has no history.
    last_compile_by_target: dict[str, dict[str, object]] = {}
    history_dao = request.app.get("job_history")
    if history_dao is not None:
        try:
            last_compile_by_target = history_dao.last_per_target()
        except Exception:
            logger.debug("job_history.last_per_target() raised; continuing")

    def _device_compile_epoch(dev: Device | None) -> int | None:
        if not dev:
            return None
        return _parse_device_compile_epoch(dev.compilation_time)

    # Build device lookup by compile_target filename
    devices_by_target: dict[str, Device] = {}
    if device_poller:
        for dev in device_poller.get_devices():
            if dev.compile_target:
                devices_by_target[dev.compile_target] = dev

    result = []
    for target in targets:
        dev = devices_by_target.get(target)
        meta = get_device_metadata(cfg.config_dir, target)
        # Detect "config changed locally" — the fallback shown when the
        # precise drift signal (``config_drifted_since_flash``, scoped to
        # the last successful flash) isn't available. Prefer `git status`
        # when the config dir is a git repo: a user's mental model of
        # "changed locally" is "`git status` shows it dirty", and mtime
        # false-positives whenever something touches the file without
        # editing it (editor autosave, `git checkout`, etc.). mtime is
        # kept only as a last resort for non-repo config dirs.
        if head_hash:
            config_modified: bool | None = target in dirty_set
        else:
            config_modified = None
            if dev and dev.compilation_time:
                try:
                    from datetime import datetime  # noqa: PLC0415
                    # compilation_time format: "Mar 29 2026, 17:00:00"
                    compile_dt = datetime.strptime(dev.compilation_time, "%b %d %Y, %H:%M:%S")
                    config_path = Path(cfg.config_dir) / target
                    if config_path.exists():
                        mtime_dt = datetime.fromtimestamp(config_path.stat().st_mtime)
                        config_modified = mtime_dt > compile_dt
                except Exception:
                    pass
        # Determine if this target has an API encryption key in its config
        has_api_key = False
        if device_poller and device_poller._encryption_keys:
            for name, _key in device_poller._encryption_keys.items():
                if device_poller._map_target(name) == target:
                    has_api_key = True
                    break

        device_mac = dev.mac_address if dev else None
        ha_configured, ha_connected, ha_device_id = _ha_status_for_target(
            ha_entity_status, target, meta, device_mac=device_mac,
            ha_mac_set=ha_mac_set, ha_mac_to_device_id=ha_mac_to_device_id,
            ha_name_to_device_id=ha_name_to_device_id,
        )

        # Bug #7 (1.6.1): MAC→IP fallback via /proc/net/arp when the
        # device poller's IP is empty/stale but we know the MAC from
        # an earlier native-API poll. ``arp.lookup`` is cheap (cached
        # for 30s) and returns None on dev hosts without the file.
        ip_with_fallback: str | None = dev.ip_address if dev else None
        source_with_fallback = dev.address_source if dev else None
        if dev and not ip_with_fallback and device_mac:
            try:
                from arp import lookup as _arp_lookup  # noqa: PLC0415
                fallback_ip = _arp_lookup(device_mac)
                if fallback_ip:
                    ip_with_fallback = fallback_ip
                    source_with_fallback = "arp"
            except Exception:
                logger.debug("ARP fallback lookup failed for %s", target, exc_info=True)

        # 4.2c: Use HA connected state as additional online signal.
        # If the device poller hasn't confirmed online yet but HA says connected,
        # treat the device as online.
        poller_online: bool | None = dev.online if dev else None
        effective_online: bool | None
        if poller_online is not True and ha_connected is True:
            effective_online = True
        else:
            effective_online = poller_online

        entry: dict = {
            "target": target,
            "friendly_name": meta["friendly_name"],
            "device_name": meta["device_name"],
            "comment": meta["comment"],
            "area": meta["area"],
            "project_name": meta["project_name"],
            "project_version": meta["project_version"],
            "online": effective_online,
            "running_version": dev.running_version if dev else None,
            "compilation_time": dev.compilation_time if dev else None,
            "config_modified": config_modified,
            # VP: if the device is pinned, compare against the pinned version
            # instead of the global server version. A pinned device at its
            # pinned version is NOT "outdated" even if the global version is newer.
            "needs_update": (
                dev.running_version != (meta.get("pinned_version") or server_version)
                if dev and dev.running_version
                else None
            ),
            # Bug #7 (1.6.1): fall back to the host's ARP table when
            # the device poller has a MAC but no fresh mDNS-derived IP.
            # Keeps the IP column populated through transient mDNS
            # outages instead of flapping to "—" until the next probe.
            "ip_address": ip_with_fallback,
            "address_source": source_with_fallback,
            "last_seen": dev.last_seen.isoformat() if dev and dev.last_seen else None,
            "server_version": server_version,
            "has_api_key": has_api_key,
            "has_web_server": meta["has_web_server"],
            "has_restart_button": meta.get("has_restart_button", False),
            "ha_configured": ha_configured,
            "ha_connected": ha_connected,
            "ha_device_id": ha_device_id,
            # #27: surface MAC so the HA custom integration can merge its
            # target-device with the native ESPHome integration's device
            # via DeviceInfo `connections={(CONNECTION_NETWORK_MAC, mac)}`.
            # Populated by the device poller (mDNS TXT or native API).
            "mac_address": device_mac,
            # #10 — network facts surfaced by the toggleable Net/IP Mode/IPv6/AP columns
            "network_type": meta.get("network_type"),
            "network_static_ip": meta.get("network_static_ip", False),
            "network_ipv6": meta.get("network_ipv6", False),
            "network_ap_fallback": meta.get("network_ap_fallback", False),
            "network_matter": meta.get("network_matter", False),
            # Bug #23: chip family + BLE proxy mode for the new Devices columns.
            "esp_type": meta.get("esp_type"),
            # UD.5: PlatformIO board string (esp32dev, nodemcu_32s, …) — secondary
            # line on the Platform column.
            "board": meta.get("board"),
            "bluetooth_proxy": meta.get("bluetooth_proxy", "off"),
            # Per-device metadata from the # esphome-fleet: comment block.
            "pinned_version": meta.get("pinned_version"),
            "schedule": meta.get("schedule"),
            "schedule_enabled": meta.get("schedule_enabled", False),
            "schedule_last_run": meta.get("schedule_last_run"),
            "schedule_once": meta.get("schedule_once"),
            # #90: IANA tz name (e.g. "America/Los_Angeles"). Absent for
            # legacy schedules; the scheduler interprets those as UTC.
            "schedule_tz": meta.get("schedule_tz"),
            "tags": meta.get("tags"),
            # Bug #16: dirty-state flag for the Devices-tab indicator.
            # True when the target's YAML has uncommitted changes
            # relative to its latest git commit.
            "has_uncommitted_changes": target in dirty_set,
            # Bug #32: per-target drift since last OTA. The hash is the
            # git HEAD at enqueue time of the most recent successful
            # flash; drift is True when that file has changed between
            # that hash and current HEAD. Null when either side is
            # unknown (no git repo, no past flash, or the target is
            # uncommitted) — UI falls back to `config_modified`, which
            # in a git repo reflects `git status` (dirty set) rather than
            # file mtime.
            "last_flashed_config_hash": last_flashed_by_target.get(target),
            "config_drifted_since_flash": (
                None if not head_hash or target not in last_flashed_by_target
                else False if last_flashed_by_target[target] == head_hash
                else target in drift_cache.get(last_flashed_by_target[target], set())
            ),
            # JH.6: tuple of (finished_at, state) for the Devices tab's
            # optional "Last compiled" column. Shape chosen so the UI
            # can render relative time + a success/failure chip without
            # a second API call. Bug #13: when the SQLite history has
            # nothing for this target (fresh server, YAML compiled by
            # another install, history evicted by retention), fall back
            # to the running device's reported ``compilation_time`` so
            # the column doesn't read "—" for devices that are obviously
            # running compiled firmware. ``source`` distinguishes the
            # two so the UI can disambiguate (history is precise; device
            # is approximate — local-tz parsed, no per-job state).
            "last_compile": (
                {
                    "at": last_compile_by_target[target]["finished_at"],
                    "state": last_compile_by_target[target]["state"],
                    "ota_result": last_compile_by_target[target].get("ota_result"),
                    "validate_only": bool(last_compile_by_target[target].get("validate_only")),
                    "download_only": bool(last_compile_by_target[target].get("download_only")),
                    "server_ota": bool(last_compile_by_target[target].get("server_ota")),
                    "source": "history",
                }
                if target in last_compile_by_target
                else (
                    {
                        "at": _device_compile_epoch(dev),
                        "state": "success",
                        "ota_result": None,
                        "validate_only": False,
                        "download_only": False,
                        "source": "device",
                    }
                    if _device_compile_epoch(dev) is not None
                    else None
                )
            ),
            # DM.1: archived flag on every active row so the UI can
            # apply ``opacity-50`` + reduced action menu uniformly. Rows
            # produced by ``scan_archived`` below carry archived=True.
            "archived": False,
        }
        result.append(entry)

    # DM.1: merge archived rows so the Devices tab can render them
    # inline (toggleable via the column-picker "Show archived devices"
    # entry). The poller / scheduler / routing engine / queue continue
    # to see only active targets — archived is purely a UI surface.
    # #203: re-read each archived YAML so the row keeps tags / area /
    # project / comment / pinned_version / schedule / network / chip /
    # BLE-proxy. Without this the Devices tab silently dropped every
    # attribute the moment a device was archived (and the tag-filter
    # pills lost the archived rows' tags entirely).
    from scanner import scan_archived  # noqa: PLC0415
    for arch in scan_archived(cfg.config_dir):
        meta = get_archived_device_metadata(cfg.config_dir, arch["filename"])
        result.append({
            "target": arch["filename"],
            "friendly_name": meta.get("friendly_name"),
            "device_name": meta.get("device_name"),
            "comment": meta.get("comment"),
            "area": meta.get("area"),
            "project_name": meta.get("project_name"),
            "project_version": meta.get("project_version"),
            "online": None,
            "running_version": None,
            "compilation_time": None,
            "config_modified": None,
            "needs_update": None,
            "ip_address": None,
            "address_source": None,
            "last_seen": None,
            "server_version": server_version,
            "has_api_key": False,
            "has_web_server": meta.get("has_web_server", False),
            "has_restart_button": meta.get("has_restart_button", False),
            "ha_configured": False,
            "ha_connected": False,
            "ha_device_id": None,
            "mac_address": None,
            "network_type": meta.get("network_type"),
            "network_static_ip": meta.get("network_static_ip", False),
            "network_ipv6": meta.get("network_ipv6", False),
            "network_ap_fallback": meta.get("network_ap_fallback", False),
            "network_matter": meta.get("network_matter", False),
            "esp_type": meta.get("esp_type"),
            "board": meta.get("board"),
            "bluetooth_proxy": meta.get("bluetooth_proxy", "off"),
            "pinned_version": meta.get("pinned_version"),
            "schedule": meta.get("schedule"),
            "schedule_enabled": meta.get("schedule_enabled", False),
            "schedule_last_run": meta.get("schedule_last_run"),
            "schedule_once": meta.get("schedule_once"),
            "schedule_tz": None,
            "tags": meta.get("tags"),
            "has_uncommitted_changes": False,
            "last_flashed_config_hash": None,
            "config_drifted_since_flash": None,
            "last_compile": None,
            "archived": True,
            "archived_at": arch["archived_at"],
            "archived_size": arch["size"],
        })

    return web.json_response(result)


@routes.get("/ui/api/queue")
async def get_queue(request: web.Request) -> web.Response:
    """Return current job queue state.

    SP.2: `log` is stripped from EVERY job in the list response. Previously
    only pending/working jobs had their log blanked; terminal jobs carried
    up to 512 KB of log text each, and 10 finished jobs on a 1 Hz SWR poll
    = ~5 MB/s steady-state. The log modal and WebSocket tail both fetch
    per-job via /ui/api/jobs/{id}/log, so the list endpoint doesn't need it.
    """
    from firmware_storage import list_variants  # noqa: PLC0415
    queue = request.app["queue"]
    jobs = []
    for job in queue.get_all():
        d = job.to_dict()
        d["log"] = None
        # #69: surface the available firmware variants up front so the
        # Queue-tab Download dropdown knows whether to show Factory,
        # OTA, or both without a second round trip. Cheap — just a dir
        # scan when has_firmware is true; skipped for every other job
        # row so the list endpoint stays small.
        if job.has_firmware:
            d["firmware_variants"] = list_variants(job.id)
        else:
            d["firmware_variants"] = []
        jobs.append(d)
    return web.json_response(jobs)


@routes.get("/ui/api/history")
async def get_history(request: web.Request) -> web.Response:
    """JH.4: list persistent compile history rows.

    Query params (all optional):
      ``target``    — filter to one YAML filename
      ``state``     — one of success / failed / timed_out / cancelled
      ``since``     — epoch seconds; rows finished before this are excluded
      ``until``     — epoch seconds; rows finished after this are excluded (bug #49)
      ``limit``     — page size, clamped to [1, 500] (default 50)
      ``offset``    — for pagination
      ``sort``      — column to sort by (bug #53); see job_history.JobHistoryDAO._SORT_COLUMNS
      ``desc``      — ``"1"`` / ``"true"`` for descending (default); anything else ascending

    The live Queue tab keeps reading from /ui/api/queue — this endpoint
    is the append-only counterpart that survives coalescing + clears.
    """
    history = request.app.get("job_history")
    if history is None:
        return web.json_response([])
    q = request.rel_url.query
    target = q.get("target") or None
    state = q.get("state") or None

    def _int(key: str) -> int | None:
        raw = q.get(key)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    since = _int("since")
    until = _int("until")
    limit = _int("limit") or 50
    offset = _int("offset") or 0

    sort_by = q.get("sort", "finished_at")
    desc_raw = (q.get("desc") or "1").lower()
    sort_desc = desc_raw in ("1", "true", "yes")

    rows = history.query(
        target=target, state=state, since=since, until=until,
        limit=limit, offset=offset,
        sort_by=sort_by, sort_desc=sort_desc,
    )
    return web.json_response(rows)


@routes.get("/ui/api/history/stats")
async def get_history_stats(request: web.Request) -> web.Response:
    """JH.4: return a compile-history rollup.

    Query params:
      ``target``       — filter to one YAML filename (optional)
      ``window_days``  — rolling window in days (default 30, max 3650)

    Response keys: total / success / failed / cancelled / timed_out /
    avg_duration_seconds / p95_duration_seconds / last_success_at /
    last_failure_at / window_days.
    """
    history = request.app.get("job_history")
    if history is None:
        return web.json_response({
            "total": 0, "success": 0, "failed": 0, "cancelled": 0, "timed_out": 0,
            "avg_duration_seconds": None, "p95_duration_seconds": None,
            "last_success_at": None, "last_failure_at": None, "window_days": 30,
        })
    q = request.rel_url.query
    target = q.get("target") or None
    try:
        window_days = int(q.get("window_days", "30"))
    except ValueError:
        window_days = 30
    return web.json_response(history.stats(target=target, window_days=window_days))


@routes.get("/ui/api/jobs/{id}/log")
async def get_job_log(request: web.Request) -> web.Response:
    """HTTP fallback for log tailing (used when WebSocket fails)."""
    job_id = request.match_info["id"]
    offset = int(request.rel_url.query.get("offset", "0"))
    queue = request.app["queue"]
    job = queue.get(job_id)
    if not job:
        return web.json_response({"error": "Job not found"}, status=404)
    finished = job.state in (JobState.SUCCESS, JobState.FAILED, JobState.TIMED_OUT)
    full_log = job.log if finished else job._streaming_log
    if full_log is None:
        full_log = ""
    chunk = full_log[offset:]
    return web.json_response({"log": chunk, "offset": len(full_log), "finished": finished})


@routes.get("/ui/api/jobs/{id}/firmware")
async def download_job_firmware(request: web.Request) -> web.Response:
    """FD.6 / #69 — download one variant of a job's firmware binary.

    Query params:
      - ``variant``: ``factory`` (first-flash image, ESP32) or ``ota``
        (smaller OTA-safe image, ESP32 + ESP8266). Defaults to the
        first variant reported by ``list_variants`` so pre-#69 callers
        (and pre-#69 legacy blobs) keep working without modification.
      - ``gz``: ``1`` to gzip-compress the response body on the fly
        and serve it with a ``.bin.gz`` filename. Useful for users
        mirroring builds to a fleet — ~30-40% smaller wire size.
    """
    import gzip as _gzip  # noqa: PLC0415
    job_id = request.match_info["id"]
    queue = request.app["queue"]
    job = queue.get(job_id)
    # Bug #1 (1.6.1): fall back to the persistent job_history table when
    # the job has been coalesced out of the live queue. The binary lives
    # on disk under /data/firmware/<job_id>.*.bin regardless of queue
    # state, so history downloads work as long as the firmware budget
    # hasn't evicted the file. ``job_target`` is needed below to build
    # the Content-Disposition filename.
    job_target: str | None
    if job is None:
        history = request.app.get("job_history")
        hist_row = history.get(job_id) if history is not None else None
        if hist_row is None:
            return web.json_response({"error": "Job not found"}, status=404)
        if not hist_row.get("has_firmware"):
            return web.json_response({"error": "Firmware not available"}, status=404)
        job_target = str(hist_row.get("target") or "")
    else:
        if not job.has_firmware:
            return web.json_response({"error": "Firmware not available"}, status=404)
        job_target = job.target

    from firmware_storage import firmware_path, list_variants, read_firmware  # noqa: PLC0415
    available = list_variants(job_id)
    if not available:
        logger.warning(
            "Job %s has_firmware=True but no variants found on disk", job_id,
        )
        return web.json_response({"error": "Firmware not available"}, status=404)

    variant = request.rel_url.query.get("variant") or available[0]
    if variant not in available:
        return web.json_response(
            {
                "error": f"Variant {variant!r} not available",
                "available": available,
            },
            status=404,
        )

    path = firmware_path(job_id, variant=variant)
    if not path.is_file():
        # list_variants said yes but the file vanished — race with
        # a concurrent delete_firmware. 404 cleanly.
        logger.warning(
            "Job %s variant=%s listed but %s is missing", job_id, variant, path,
        )
        return web.json_response({"error": "Firmware not available"}, status=404)

    target_name = job_target or "job"
    stem = target_name.removesuffix(".yaml").removesuffix(".yml") or target_name
    # Surface the variant in the filename so a user who downloads both
    # doesn't end up with two indistinguishable `.bin`s in their
    # browser's Downloads folder.
    if variant == "firmware":
        filename = f"{stem}-{job_id[:8]}.bin"  # legacy shape (no variant tag)
    else:
        filename = f"{stem}-{job_id[:8]}-{variant}.bin"

    gz_requested = request.rel_url.query.get("gz") in ("1", "true", "yes")
    if gz_requested:
        # Read + gzip-compress in memory. Firmware binaries are 1-2MB so
        # buffering is cheap, and a streaming compressor adds complexity
        # without a material benefit at this scale. Use gzip.compress
        # with the default compresslevel (9) — saves ~30-40% on typical
        # ESP firmware at ~30ms CPU per job. Served with
        # Content-Encoding: identity and a .gz filename so the browser
        # saves the compressed bytes literally instead of auto-decoding.
        raw = read_firmware(job_id, variant=variant)
        if raw is None:
            return web.json_response({"error": "Firmware not available"}, status=404)
        compressed = _gzip.compress(raw)
        return web.Response(
            body=compressed,
            headers={
                "Content-Type": "application/gzip",
                "Content-Disposition": f'attachment; filename="{filename}.gz"',
                "Content-Encoding": "identity",
            },
        )

    return web.FileResponse(
        path=path,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@routes.get("/ui/api/jobs/{id}/firmware-variants")
async def list_job_firmware_variants(request: web.Request) -> web.Response:
    """#69 — enumerate the firmware variants stored for a job.

    Driven by the Queue-tab Download dropdown: the UI polls this once
    per job-row on modal open to know which options (Factory / OTA,
    plus the .gz toggle) to render. Cheap to compute — just a dir scan.
    """
    from firmware_storage import list_variants  # noqa: PLC0415
    job_id = request.match_info["id"]
    queue = request.app["queue"]
    job = queue.get(job_id)
    # Bug #1 (1.6.1): the UI calls this from the history surfaces too,
    # so fall back to job_history for jobs that have already been
    # coalesced out of the live queue. The variant list itself is
    # filesystem-derived (see :func:`firmware_storage.list_variants`),
    # so no DB hit is needed for the variant data — we just confirm
    # that the id is a real job before exposing the directory scan.
    if job is None:
        history = request.app.get("job_history")
        hist_row = history.get(job_id) if history is not None else None
        if hist_row is None:
            return web.json_response({"error": "Job not found"}, status=404)
    return web.json_response({"variants": list_variants(job_id)})


@routes.get("/ui/api/targets/{filename}/logs/ws")
async def ws_device_log(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for streaming live device logs via the native API."""
    filename = request.match_info["filename"]
    device_poller = request.app.get("device_poller")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    if not device_poller:
        await ws.send_str("Device poller not available\n")
        await ws.close()
        return ws

    # Find the device for this target
    dev = None
    for d in device_poller.get_devices():
        if d.compile_target == filename:
            dev = d
            break

    if not dev or not dev.ip_address:
        # DL.4: the user-facing "Device not found" error is cryptic when
        # the real issue is a scanner/name-mapping regression. Dump the
        # poller's current view so operators can see whether the target
        # is absent entirely (→ scanner failure, look for a DL.1 WARNING),
        # present but without an IP (→ resolution issue, look at DL.2),
        # or mapped under a slightly different device_name (→ name-
        # normalization). Kept at INFO so it's visible without debug.
        known = [
            {
                "name": d.name,
                "compile_target": d.compile_target,
                "ip_address": d.ip_address,
                "online": d.online,
            }
            for d in device_poller.get_devices()
        ]
        logger.info(
            "ws_device_log: no device for target %r. Poller knows %d "
            "device(s): %s",
            filename, len(known), known,
        )
        await ws.send_str(f"Device not found or no IP address for {filename}\n")
        await ws.close()
        return ws

    noise_psk = device_poller._encryption_keys.get(dev.name)
    addr = device_poller._address_overrides.get(dev.name) or dev.ip_address
    # Bug #11 (1.6.1): if we end up with no key for a device the YAML
    # declared one for, the usual symptom is ``Connection requires
    # encryption`` from the device. That message lands in the user's
    # terminal; this log line lands in the add-on log so support
    # threads can point at the right triage endpoint in one round
    # trip instead of asking for a code dive.
    if noise_psk is None and dev.online:
        logger.debug(
            "ws_device_log: %s has no cached noise_psk. If the device "
            "declares api.encryption.key the scan may have run before "
            "ESPHome finished installing — check "
            "GET /ui/api/targets/%s/api-key and restart the add-on if "
            "it 404s.", dev.name, filename,
        )

    import asyncio as _asyncio  # noqa: PLC0415
    import aioesphomeapi  # noqa: PLC0415
    from aioesphomeapi import LogLevel  # noqa: PLC0415
    from typing import Any  # noqa: PLC0415

    client = aioesphomeapi.APIClient(addr, 6053, password=None, noise_psk=noise_psk)
    unsub = None

    try:
        await ws.send_str(f"Connecting to {dev.name} at {addr}...\n")
        await client.connect(login=True)
        await ws.send_str("Connected. Streaming logs...\n\n")

        # C.8: capture the running loop while we're inside the async context.
        # The log_callback fires from a different thread (aioesphomeapi worker
        # thread), so we cannot call asyncio.get_running_loop() from inside it.
        loop = _asyncio.get_running_loop()

        def log_callback(msg: Any) -> None:
            if ws.closed:
                return
            from datetime import datetime as _dt  # noqa: PLC0415
            text = msg.message.decode("utf-8", errors="replace")
            if not text.endswith("\n"):
                text += "\n"
            ts = _dt.now().strftime("[%H:%M:%S] ")
            # ``run_coroutine_threadsafe`` is the cross-thread analogue of
            # create_task — required because log_callback runs in a worker
            # thread, not on the event loop thread. ``ensure_future(..., loop=)``
            # was the legacy way and is removed in 3.12.
            _asyncio.run_coroutine_threadsafe(ws.send_str(ts + text), loop)

        # subscribe_logs is synchronous and returns an unsubscribe callable.
        unsub = client.subscribe_logs(log_callback, log_level=LogLevel.LOG_LEVEL_VERY_VERBOSE, dump_config=True)

        # Keep the WebSocket open until the browser disconnects.
        async for msg in ws:
            if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    except Exception as exc:
        logger.debug("Device log WebSocket error for %s: %s", filename, exc)
        try:
            await ws.send_str(f"\nConnection error: {exc}\n")
        except Exception:
            pass
    finally:
        if unsub is not None:
            try:
                unsub()
            except Exception:
                pass
        try:
            await client.disconnect()
        except Exception:
            pass

    return ws


@routes.get("/ui/api/ws/events")
async def ws_events(request: web.Request) -> web.WebSocketResponse:
    """State-change event stream (#41).

    Any client (typically the HA custom integration's coordinator) can
    connect and receive JSON events whenever something changes on the
    server — queue mutations, worker registrations, device discoveries,
    scanner picks-up-new-YAML. Enables real-time entity updates in HA
    without waiting for the 30 s coordinator poll.

    Protocol: server → client JSON messages of the form
    ``{"type": "queue_changed"|"workers_changed"|"targets_changed"|
    "devices_changed", ...}``. No client → server messages expected;
    pings are handled by aiohttp's autoping.
    """
    import asyncio as _asyncio  # noqa: PLC0415
    from event_bus import subscribe, unsubscribe  # noqa: PLC0415

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    queue = subscribe()
    try:
        # Send an immediate "hello" so clients can distinguish "connected,
        # no events yet" from "not yet connected".
        await ws.send_json({"type": "hello"})
        while not ws.closed:
            try:
                message = await _asyncio.wait_for(queue.get(), timeout=60.0)
            except _asyncio.TimeoutError:
                # Autoping keeps the connection alive; this just lets us
                # check ws.closed and exit cleanly if the peer vanished.
                continue
            try:
                await ws.send_json(message)
            except ConnectionError:
                break
    finally:
        unsubscribe(queue)
    return ws


@routes.get("/ui/api/workers/{id}/logs")
async def get_worker_logs_snapshot(request: web.Request) -> web.Response:
    """WL.3 hydration: plain-text dump of whatever the broker has buffered.

    Called once by the UI when the dialog opens. The WS path is what
    delivers live lines after the initial snapshot.
    """
    client_id = request.match_info["id"]
    broker = request.app.get("worker_log_broker")
    body = broker.snapshot(client_id) if broker else ""
    return web.Response(
        body=body.encode("utf-8"),
        content_type="text/plain",
        charset="utf-8",
    )


@routes.get("/ui/api/workers/{id}/logs/ws")
async def ws_worker_logs(request: web.Request) -> web.WebSocketResponse:
    """WL.3 live tail: open WS == 'user is watching this worker'.

    The first frame sent is the broker's current buffer snapshot
    (hydration). Every subsequent frame is a live push fanned out by
    the broker. Combining hydration and live tail into one ordered
    stream removes the race where a separate GET snapshot and a
    parallel WS subscribe both observed the same chunk — the UI
    would then write those lines twice.

    The subscriber count drives ``stream_logs`` in the control-poll /
    heartbeat response. Closing this WS decrements the count, which
    flips the worker's pusher off within ~1 s.
    """
    client_id = request.match_info["id"]
    broker = request.app.get("worker_log_broker")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    if broker is None:
        await ws.close()
        return ws

    # Atomic subscribe + snapshot — no await between reading the
    # buffer and adding ws to subscribers, so no concurrent
    # append_async can interleave.
    snapshot = broker.subscribe_and_snapshot(client_id, ws)
    try:
        if snapshot:
            await ws.send_str(snapshot)
        async for msg in ws:
            if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
            # Browser keep-alive pings are otherwise ignored.
    finally:
        broker.unsubscribe(client_id, ws)

    return ws


@routes.get("/ui/api/jobs/{id}/log/ws")
async def ws_browser_log(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for browser live log tailing."""
    job_id = request.match_info["id"]
    queue = request.app["queue"]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Send any log content already buffered (streaming or persisted)
    job = queue.get(job_id)
    if job:
        existing = job._streaming_log or job.log or ""
        if existing:
            await ws.send_str(existing)

    # Subscribe for new lines produced while we are connected
    subscribers: dict = request.app.setdefault("log_subscribers", {})
    subscribers.setdefault(job_id, set()).add(ws)

    try:
        async for msg in ws:
            if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
            # Browser may send a keep-alive ping; all other messages are ignored
    finally:
        subscribers.get(job_id, set()).discard(ws)
        if job_id in subscribers and not subscribers[job_id]:
            del subscribers[job_id]

    return ws


async def _get_workers_response(request: web.Request) -> web.Response:
    """Return list of registered build workers with online status."""
    registry = request.app["registry"]
    queue = request.app["queue"]
    from settings import get_settings as _gs  # noqa: PLC0415
    s = _gs()
    threshold = s.worker_offline_threshold
    # DQ.5: surface the effective per-worker quota alongside the persisted
    # override so the UI can render "Custom: 5 GiB" vs "Default: 10 GiB"
    # without a second fetch. The fleet default is also returned at the
    # row level (cheap, lets the ConnectWorkerModal default-radio render
    # without a separate /ui/api/settings call).
    default_quota = s.default_worker_disk_quota_bytes

    result = []
    for worker in registry.get_all():
        d = worker.to_dict()
        d["online"] = registry.is_online(worker.client_id, threshold)
        if d.get("current_job_id"):
            job = queue.get(d["current_job_id"])
            if job:
                d["current_job_target"] = job.target
        d["disk_quota_bytes"] = worker.effective_disk_quota_bytes(default_quota)
        d["default_worker_disk_quota_bytes"] = default_quota
        result.append(d)
    return web.json_response(result)


@routes.get("/ui/api/workers")
async def get_workers(request: web.Request) -> web.Response:
    """Return list of registered build workers with online status."""
    return await _get_workers_response(request)


@routes.get("/ui/api/clients")
async def get_clients(request: web.Request) -> web.Response:
    """Legacy alias for /ui/api/workers — kept for backwards compatibility."""
    return await _get_workers_response(request)


@routes.get("/ui/api/devices")
async def get_devices(request: web.Request) -> web.Response:
    """Return known ESPHome devices with version info.

    Enriches every device — managed *and* unmanaged — with HA configured /
    connected state by cross-referencing the device MAC and name against the
    HA entity registry snapshot. This lets the UI distinguish "random mDNS
    broadcast we happened to pick up" from "real ESPHome device HA also
    knows about, but we don't have its YAML yet" on the unmanaged rows.
    """
    device_poller = request.app.get("device_poller")
    server_version = get_esphome_version()
    ha_entity_status: dict[str, dict] = request.app["_rt"].get("ha_entity_status", {})
    ha_mac_set: set[str] = request.app["_rt"].get("ha_mac_set", set())
    ha_mac_to_device_id: dict[str, str] = request.app["_rt"].get("ha_mac_to_device_id", {})
    ha_name_to_device_id: dict[str, str] = request.app["_rt"].get("ha_name_to_device_id", {})

    if not device_poller:
        return web.json_response([])

    result = []
    for dev in device_poller.get_devices():
        d = dev.to_dict()
        d["server_version"] = server_version
        d["needs_update"] = (
            dev.running_version != server_version
            if dev.running_version
            else None
        )

        # Cross-reference against HA. We synthesise a minimal ``meta`` so we
        # can reuse the same matcher the targets endpoint uses — no
        # friendly_name, just the raw device name.
        meta = {"device_name_raw": dev.name}
        ha_configured, ha_connected, ha_device_id = _ha_status_for_target(
            ha_entity_status,
            target=dev.name,
            meta=meta,
            device_mac=dev.mac_address,
            ha_mac_set=ha_mac_set,
            ha_mac_to_device_id=ha_mac_to_device_id,
            ha_name_to_device_id=ha_name_to_device_id,
        )
        d["ha_configured"] = ha_configured
        d["ha_connected"] = ha_connected
        d["ha_device_id"] = ha_device_id
        result.append(d)

    return web.json_response(result)


@routes.get("/ui/api/esphome-versions")
async def get_esphome_versions(request: web.Request) -> web.Response:
    """Return ESPHome version state: selected, detected, and available list."""
    selected = get_esphome_version()
    detected = request.app["_rt"].get("esphome_detected_version")
    available = request.app["_rt"].get("esphome_available_versions", [])

    # If PyPI list is empty, at least include the currently selected version so
    # the UI has something to show.
    if not available and selected and selected != "unknown":
        available = [selected]

    return web.json_response({
        "selected": selected,
        "detected": detected,
        "available": available,
    })


@routes.post("/ui/api/esphome-versions/refresh")
async def refresh_esphome_versions(request: web.Request) -> web.Response:
    """Force-refresh the PyPI ESPHome version list (bug #19).

    Bypasses the 1-hour server-side TTL so that the Refresh button in the
    header dropdown actually hits PyPI and returns the latest releases —
    previously the UI just re-polled our cached list and showed the same
    versions it already had.
    """
    # Import here to avoid a circular import at module load time.
    from main import _fetch_pypi_versions  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    async with aiohttp.ClientSession() as session:
        versions = await _fetch_pypi_versions(session)

    if versions:
        request.app["_rt"]["esphome_available_versions"] = versions
        request.app["_rt"]["esphome_versions_fetched_at"] = _time.monotonic()
        logger.info("UI-triggered PyPI refresh: %d versions", len(versions))
    else:
        logger.warning("UI-triggered PyPI refresh returned no versions")

    selected = get_esphome_version()
    detected = request.app["_rt"].get("esphome_detected_version")
    available = versions or request.app["_rt"].get("esphome_available_versions", [])
    return web.json_response({
        "selected": selected,
        "detected": detected,
        "available": available,
    })


@routes.post("/ui/api/esphome-version")
async def set_esphome_version_handler(request: web.Request) -> web.Response:
    """Set the active ESPHome version for new compile jobs.

    Body: { "version": "2026.3.1" }

    Bug #105: also schedules `ensure_esphome_installed` so the version
    picker is a recovery path for a stuck install. Previously this
    handler updated the selected version without scheduling the install,
    which meant a user on a fresh HAOS box with no bundled ESPHome and
    no builder add-on had no way to unblock the "Installing ESPHome…"
    banner from the UI.
    """
    import asyncio as _asyncio  # noqa: PLC0415
    import scanner as _scanner  # noqa: PLC0415

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    version = body.get("version", "").strip()
    if not version:
        return web.json_response({"error": "version is required"}, status=400)

    set_esphome_version(version)
    _scanner._esphome_install_failed = False
    _scanner._esphome_ready.clear()

    loop = _asyncio.get_running_loop()
    loop.run_in_executor(None, _scanner.ensure_esphome_installed, version)
    logger.info("ESPHome version changed to %s via UI; install scheduled", version)
    return web.json_response({"ok": True, "version": version})


@routes.post("/ui/api/esphome/reinstall")
async def reinstall_esphome(request: web.Request) -> web.Response:
    """SE.8: retry the server-side ESPHome lazy install.

    Wired to the "Retry" button on the top-of-app install banner. The
    handler schedules `ensure_esphome_installed` for the currently
    selected version in an executor and returns immediately — the
    install runs in the background; the UI polls /ui/api/server-info
    for the transition from `failed` or `installing` → `ready`.
    """
    import asyncio as _asyncio  # noqa: PLC0415
    import scanner as _scanner  # noqa: PLC0415

    target_version = _scanner.get_esphome_version()
    if target_version in ("unknown", "installing"):
        return web.json_response(
            {"error": "No target ESPHome version known yet"}, status=409,
        )
    # Clear the failure flag so get_esphome_version reports "installing"
    # while this new attempt is in flight.
    _scanner._esphome_install_failed = False
    _scanner._esphome_ready.clear()

    # run_in_executor returns a Future that's already scheduled; we don't
    # need create_task around it. Fire-and-forget is fine since the UI
    # polls server-info for the status transition.
    loop = _asyncio.get_running_loop()
    loop.run_in_executor(None, _scanner.ensure_esphome_installed, target_version)
    logger.info("Retrying ESPHome %s install via /ui/api/esphome/reinstall", target_version)
    return web.json_response({"ok": True, "version": target_version})


@routes.post("/ui/api/validate")
async def validate_config(request: web.Request) -> web.Response:
    """Validate a target's config by running ``esphome config`` directly
    on the server.

    Body: { "target": "mydevice.yaml" }
    Returns: { "success": true/false, "output": "..." }

    Bug #25: validation now runs as a direct subprocess on the add-on
    server instead of going through the job queue. Rationale:
      - ``esphome config`` only reads YAML files that are already on the
        server's filesystem — no bundle transfer, no worker needed.
      - It's fast (2–5 s) and the result is returned immediately in the
        HTTP response — no queue polling, no log modal, no streaming.
      - Doesn't consume remote worker capacity.
    """
    import asyncio as _asyncio  # noqa: PLC0415

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    target = body.get("target")
    if not target:
        return web.json_response({"error": "target required"}, status=400)

    cfg = _cfg(request)
    config_path = safe_resolve(Path(cfg.config_dir), target)
    if config_path is None or not config_path.exists():
        return json_error("Target file not found", 404)

    # #103: ``secrets.yaml`` is a flat key/value dict consumed by
    # ``!secret`` references in other YAMLs — it isn't itself an ESPHome
    # device config, and ``esphome config secrets.yaml`` fails with a
    # schema error because the top level is missing ``esphome:`` /
    # ``esp32:`` etc. The ESPHome Dashboard's own editor treats
    # ``secrets.yaml`` the same way (no "Validate" action). Mirror that:
    # return success with an explanatory note so the UI's Validate
    # button doesn't light up red on an intentional-shape file.
    if Path(target).name == "secrets.yaml":
        return web.json_response({
            "success": True,
            "output": (
                "secrets.yaml holds !secret values — ESPHome's config "
                "validator doesn't apply to it (no device schema). "
                "Skipped.\n"
            ),
            "skipped": True,
        })

    # #84: use the correct ESPHome version for validation. If the device is
    # pinned, install that version via the version manager and validate with
    # its binary — not the server's default. This ensures pinned devices
    # validate against the version they'll actually compile with.
    # #48: compare the pin against the ACTUAL installed binary, not the
    # tracked "selected" version (pypi_version_refresher updates the
    # selected version from the HA Supervisor's ESPHome add-on, which can
    # differ from the version bundled in our own container). Otherwise a
    # pin matching the "selected" version silently skips the version-
    # manager install and uses the wrong binary.
    import scanner as _scanner  # noqa: PLC0415
    from scanner import _get_installed_esphome_version  # noqa: PLC0415
    meta = read_device_meta(cfg.config_dir, target)
    pin = meta.get("pin_version")
    # SE.6: default to the lazy-installed venv binary when ready. Pin
    # code path below still runs VersionManager for pinned devices, so
    # those remain decoupled from the server's tracked version.
    if _scanner._esphome_ready.is_set() and _scanner._server_esphome_bin:
        esphome_bin = _scanner._server_esphome_bin
    else:
        # Pre-SE.1 transitional / test-harness fallback: the bundled
        # `esphome` binary on PATH.
        esphome_bin = "esphome"
    installed_binary_version = _get_installed_esphome_version()

    # SE.6: when no pin is set and the venv isn't ready yet, return
    # 503 so the UI can surface "please retry in a moment" instead of
    # shelling into a binary that doesn't exist. Pinned-device path
    # installs its own version via VersionManager regardless.
    if not pin and not _scanner._esphome_ready.is_set():
        # Last-chance: check if the bundled package provides `esphome`
        # on PATH — covers the pre-SE.1 state where no lazy install is
        # needed to validate.
        import shutil as _shutil  # noqa: PLC0415
        if _shutil.which("esphome") is None:
            return web.json_response(
                {
                    "success": False,
                    "output": "ESPHome still installing, please retry in a moment",
                },
                status=503,
            )

    if pin and pin != installed_binary_version:
        try:
            logger.info("Validating %s: ensuring ESPHome %s is installed for pinned version", target, pin)
            esphome_bin = await _ensure_pinned_esphome_bin(pin)
        except Exception as exc:
            logger.warning("Could not install pinned ESPHome %s for validation: %s", pin, exc)
            # Fall back to server default

    logger.info("Validating %s via %s config (direct subprocess)", target, esphome_bin)

    try:
        proc = await _asyncio.create_subprocess_exec(
            esphome_bin, "config", str(config_path),
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.STDOUT,
            cwd=cfg.config_dir,
        )
        stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        success = proc.returncode == 0
    except _asyncio.TimeoutError:
        return web.json_response(
            {"success": False, "output": "Validation timed out after 60 seconds"},
            status=200,
        )
    except FileNotFoundError:
        return web.json_response(
            {"success": False, "output": "esphome binary not found on the server"},
            status=500,
        )
    except Exception as exc:
        logger.exception("Validation subprocess failed for %s", target)
        return web.json_response(
            {"success": False, "output": f"Internal error: {exc}"},
            status=500,
        )

    if success:
        logger.info("Validation passed for %s", target)
    else:
        logger.warning("Validation failed for %s (exit %d)", target, proc.returncode or -1)
    return web.json_response({"success": success, "output": output})


# ---------------------------------------------------------------------------
# RC.1 — rendered-config endpoint
# ---------------------------------------------------------------------------

# RC.1: in-process LRU cache for rendered configs. Key = (filename,
# file_mtime_ns, secrets_mtime_ns) so any save/commit on the file or its
# secrets.yaml busts the entry automatically (a fresh stat returns a
# different mtime → cache miss → fresh subprocess run). Capped at 32
# entries with FIFO eviction so a long-running session doesn't grow it
# unbounded. Single-process server so a plain dict is sufficient.
_rendered_config_cache: "OrderedDict[tuple[str, int, int], tuple[bool, str]] | None" = None
_RENDERED_CONFIG_CACHE_MAX = 32

# ESPHome wraps `!secret` references in ANSI conceal escapes
# (`\x1b[8m...\x1b[28m`) so terminals visually mask them; Monaco renders
# the literal escape bytes as garbage text. Strip the full SGR family
# (CSI ... letter) since `esphome config` may also emit colour codes
# from its `_LOGGER` chatter that we want to clean before display.
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _get_rendered_cache() -> "OrderedDict[tuple[str, int, int], tuple[bool, str]]":
    """Lazy-init the module-level OrderedDict so test harnesses that
    re-import this module land on a fresh cache."""
    global _rendered_config_cache
    if _rendered_config_cache is None:
        _rendered_config_cache = OrderedDict()
    return _rendered_config_cache


def _rendered_cache_key(config_dir: str, filename: str) -> tuple[str, int, int]:
    """Build the cache key for *filename* under *config_dir*. The
    secrets.yaml mtime participates so a `!secret` value change re-runs
    `esphome config` even when the device YAML is byte-identical."""
    file_mtime = 0
    secrets_mtime = 0
    try:
        file_path = safe_resolve(Path(config_dir), filename)
        if file_path is not None and file_path.exists():
            file_mtime = file_path.stat().st_mtime_ns
    except Exception:
        file_mtime = 0
    try:
        secrets_path = Path(config_dir) / "secrets.yaml"
        if secrets_path.exists():
            secrets_mtime = secrets_path.stat().st_mtime_ns
    except Exception:
        secrets_mtime = 0
    return (filename, file_mtime, secrets_mtime)


@routes.get("/ui/api/targets/{filename}/rendered-config")
async def get_rendered_config(request: web.Request) -> web.Response:
    """RC.1 — return the YAML *as ESPHome will compile it* for *filename*.

    Runs ``esphome config <abs-path>`` (same venv-binary discovery as
    ``/ui/api/validate``), captures stdout (the rendered YAML on
    success) or the captured output (validation/parse errors on
    failure), and returns ``{"success": bool, "output": str,
    "cached": bool}``.

    The rendered output resolves ``!include`` / ``!secret`` /
    ``substitutions:`` / ``packages:`` / ``<<: *anchor`` merges to
    their final values — including plaintext ``!secret`` values, which
    is exactly the diagnostic context the user wants but means the
    response *must not* be logged server-side. Logger calls below
    intentionally never reference ``output``.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    config_path = safe_resolve(Path(cfg.config_dir), filename)
    if config_path is None or not config_path.exists():
        return json_error("Target not found", 404)

    # secrets.yaml itself isn't a device config — same skip the
    # validate endpoint applies for the same reason.
    if Path(filename).name == "secrets.yaml":
        return web.json_response({
            "success": True,
            "output": (
                "secrets.yaml holds !secret values — there is no device "
                "config to render here. Open a device YAML to view its "
                "rendered output.\n"
            ),
            "cached": False,
            "skipped": True,
        })

    cache = _get_rendered_cache()
    key = _rendered_cache_key(cfg.config_dir, filename)
    hit = cache.get(key)
    if hit is not None:
        # LRU bump so the most recently viewed entries stay warmest.
        cache.move_to_end(key)
        success, output = hit
        return web.json_response({"success": success, "output": output, "cached": True})

    # Same ESPHome-binary discovery as /ui/api/validate so a pinned
    # device's rendered view matches what its compile would produce.
    import scanner as _scanner  # noqa: PLC0415
    from scanner import _get_installed_esphome_version  # noqa: PLC0415
    meta = read_device_meta(cfg.config_dir, filename)
    pin = meta.get("pin_version")
    if _scanner._esphome_ready.is_set() and _scanner._server_esphome_bin:
        esphome_bin = _scanner._server_esphome_bin
    else:
        esphome_bin = "esphome"
    installed_binary_version = _get_installed_esphome_version()

    if not pin and not _scanner._esphome_ready.is_set():
        import shutil as _shutil  # noqa: PLC0415
        if _shutil.which("esphome") is None:
            return web.json_response(
                {
                    "success": False,
                    "output": "ESPHome still installing, please retry in a moment",
                    "cached": False,
                },
                status=503,
            )

    if pin and pin != installed_binary_version:
        try:
            logger.info("Rendering %s: ensuring ESPHome %s is installed for pinned version", filename, pin)
            esphome_bin = await _ensure_pinned_esphome_bin(pin)
        except Exception as exc:
            logger.warning("Could not install pinned ESPHome %s for rendering: %s", pin, exc)

    logger.info("Rendering %s via %s config (direct subprocess)", filename, esphome_bin)
    try:
        # Bug #113: capture stdout and stderr separately. ESPHome's
        # `command_config` (esphome/__main__.py) writes the rendered
        # YAML to stdout via `safe_print` and writes its `_LOGGER`
        # status chatter ("INFO ESPHome 2026.4.3", "INFO Reading
        # configuration...", "WARNING GPIO12 is a strapping PIN...",
        # trailing "INFO Configuration is valid!") to stderr. Pre-#113
        # we used `stderr=STDOUT` which merged them, so the modal
        # showed the YAML buried between INFO/WARNING lines. Now we
        # show the user just the YAML on success and surface stderr
        # only when the render fails (where the diagnostic context
        # belongs).
        proc = await asyncio.create_subprocess_exec(
            esphome_bin, "config", str(config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cfg.config_dir,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=60)
        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        success = proc.returncode == 0
        if success:
            output = _ANSI_SGR_RE.sub("", stdout)
        else:
            # On failure, the validation error lives in stderr (cv.Invalid
            # messages, traceback). stdout may also carry partial render
            # output from a YAML that started parsing then errored mid-
            # tree, so concatenate to give the user the full diagnostic
            # context. Stderr first because the actionable message lives there.
            output = _ANSI_SGR_RE.sub("", stderr + (stdout if stdout else ""))
    except asyncio.TimeoutError:
        return web.json_response(
            {"success": False, "output": "Rendering timed out after 60 seconds", "cached": False},
            status=200,
        )
    except FileNotFoundError:
        return web.json_response(
            {"success": False, "output": "esphome binary not found on the server", "cached": False},
            status=500,
        )
    except Exception as exc:
        logger.exception("Render subprocess failed for %s", filename)
        return web.json_response(
            {"success": False, "output": f"Internal error: {exc}", "cached": False},
            status=500,
        )

    if success:
        # NEVER log the rendered output — it carries plaintext !secret
        # values. Length is fine; content is not.
        logger.info("Rendered config produced for %s (%d bytes)", filename, len(output))
    else:
        logger.warning("Render failed for %s (exit %d)", filename, proc.returncode or -1)

    # Cache after the run so the same edit-session's repeat opens are
    # subprocess-free. Successful AND failed renders both cache so a
    # known-bad YAML doesn't burn a fresh subprocess on every retry.
    cache[key] = (success, output)
    cache.move_to_end(key)
    while len(cache) > _RENDERED_CONFIG_CACHE_MAX:
        cache.popitem(last=False)

    return web.json_response({"success": success, "output": output, "cached": False})


# ---------------------------------------------------------------------------
# Per-device metadata + schedule + version pinning endpoints
# ---------------------------------------------------------------------------

@routes.post("/ui/api/targets/{filename}/pin")
async def pin_target_version(request: web.Request) -> web.Response:
    """Pin a device to a specific ESPHome version.

    Body: ``{"version": "2026.3.3"}``
    The pin is stored in the ``# esphome-fleet:`` comment block.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None or not path.exists():
        return json_error("Target not found", 404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    version = body.get("version", "").strip()
    if not version:
        return web.json_response({"error": "version required"}, status=400)

    meta = read_device_meta(cfg.config_dir, filename)
    meta["pin_version"] = version
    write_device_meta(cfg.config_dir, filename, meta)
    logger.info("Pinned %s to version %s%s", filename, version, _who(request))
    from git_versioning import commit_file  # noqa: PLC0415
    await commit_file(Path(cfg.config_dir), filename, "pin")
    return web.json_response({"ok": True, "pinned_version": version})


@routes.delete("/ui/api/targets/{filename}/pin")
async def unpin_target_version(request: web.Request) -> web.Response:
    """Remove the version pin from a device."""
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None or not path.exists():
        return json_error("Target not found", 404)

    meta = read_device_meta(cfg.config_dir, filename)
    meta.pop("pin_version", None)
    write_device_meta(cfg.config_dir, filename, meta)
    logger.info("Unpinned %s%s", filename, _who(request))
    from git_versioning import commit_file  # noqa: PLC0415
    await commit_file(Path(cfg.config_dir), filename, "unpin")
    return web.json_response({"ok": True})

@routes.post("/ui/api/targets/{filename}/meta")
async def update_target_meta(request: web.Request) -> web.Response:
    """Update arbitrary per-device metadata stored in the YAML comment block.

    Body: dict of key→value. ``null`` values delete the key.
    E.g. ``{"pin_version": "2026.3.3", "tags": "office"}``
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None or not path.exists():
        return json_error("Target not found", 404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Expected a JSON object"}, status=400)

    meta = read_device_meta(cfg.config_dir, filename)
    for key, value in body.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    write_device_meta(cfg.config_dir, filename, meta)
    logger.info("Updated metadata for %s: %s%s", filename, list(body.keys()), _who(request))
    # Bug #22: derive a specific commit-message action from the body so
    # the git log says what actually changed (e.g. "Updated device tags"
    # rather than "Updated device metadata"). Single-key bodies get a
    # per-key subject; multi-key bodies fall back to the generic "meta".
    if len(body) == 1:
        only_key = next(iter(body.keys()))
        cleared = body[only_key] is None
        action = f"meta {only_key}{' cleared' if cleared else ''}"
    else:
        action = "meta"
    from git_versioning import commit_file  # noqa: PLC0415
    await commit_file(Path(cfg.config_dir), filename, action)
    return web.json_response({"ok": True})


@routes.post("/ui/api/targets/{filename}/schedule")
async def set_target_schedule(request: web.Request) -> web.Response:
    """Set a cron schedule for automatic compile+OTA on a device.

    Body: ``{"cron": "0 2 * * 0"}``
    Returns: ``{"ok": true, "schedule": "...", "schedule_enabled": true}``
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None or not path.exists():
        return json_error("Target not found", 404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    cron_expr = body.get("cron", "").strip()
    if not cron_expr:
        return web.json_response({"error": "cron expression required"}, status=400)

    # #90: optional `tz` (IANA name like "America/Los_Angeles"). When set,
    # the scheduler interprets the cron expression in that tz. Absent → UTC.
    tz = body.get("tz")
    if tz is not None and not isinstance(tz, str):
        return web.json_response({"error": "tz must be a string"}, status=400)
    if tz:
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415
            ZoneInfo(tz)  # raises ZoneInfoNotFoundError if unknown
        except Exception as exc:
            return web.json_response({"error": f"Invalid tz: {exc}"}, status=400)

    # Validate the cron expression.
    try:
        from croniter import croniter  # type: ignore[import-untyped]  # noqa: PLC0415
        croniter(cron_expr)  # raises ValueError if invalid
    except ValueError as exc:
        return web.json_response({"error": f"Invalid cron expression: {exc}"}, status=400)
    except ImportError:
        # croniter not installed — accept the expression unvalidated rather
        # than blocking the feature. The scheduler will log when it can't parse.
        pass

    meta = read_device_meta(cfg.config_dir, filename)
    meta["schedule"] = cron_expr
    meta["schedule_enabled"] = True
    if tz:
        meta["schedule_tz"] = tz
    else:
        # No tz sent: clear any stale tz so the scheduler falls back to UTC.
        meta.pop("schedule_tz", None)
    write_device_meta(cfg.config_dir, filename, meta)
    import scheduler as _sched  # noqa: PLC0415
    _sched.sync_target(filename)
    logger.info("Schedule set for %s: %s (tz=%s)%s", filename, cron_expr, tz or "UTC", _who(request))
    from git_versioning import commit_file  # noqa: PLC0415
    await commit_file(Path(cfg.config_dir), filename, "schedule")
    return web.json_response({
        "ok": True,
        "schedule": cron_expr,
        "schedule_enabled": True,
        "schedule_tz": tz,
    })


@routes.delete("/ui/api/targets/{filename}/schedule")
async def delete_target_schedule(request: web.Request) -> web.Response:
    """Remove any schedule (recurring or one-time) from a device.

    #37: previously this only removed the recurring ``schedule`` fields
    (``schedule``, ``schedule_enabled``, ``schedule_last_run``) but left
    ``schedule_once`` intact, so clicking "Remove schedule" on a device
    that had a one-time schedule appeared to succeed but the schedule
    stuck around. Now removes both types.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None or not path.exists():
        return json_error("Target not found", 404)

    meta = read_device_meta(cfg.config_dir, filename)
    meta.pop("schedule", None)
    meta.pop("schedule_enabled", None)
    meta.pop("schedule_last_run", None)
    meta.pop("schedule_once", None)
    meta.pop("schedule_tz", None)
    write_device_meta(cfg.config_dir, filename, meta)
    import scheduler as _sched  # noqa: PLC0415
    _sched.sync_target(filename)
    logger.info("Schedule removed for %s%s", filename, _who(request))
    from git_versioning import commit_file  # noqa: PLC0415
    await commit_file(Path(cfg.config_dir), filename, "unschedule")
    return web.json_response({"ok": True})


@routes.post("/ui/api/targets/{filename}/schedule/toggle")
async def toggle_target_schedule(request: web.Request) -> web.Response:
    """Toggle the schedule enabled/disabled without clearing the expression."""
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None or not path.exists():
        return json_error("Target not found", 404)

    meta = read_device_meta(cfg.config_dir, filename)
    if not meta.get("schedule"):
        return web.json_response({"error": "No schedule configured"}, status=400)
    meta["schedule_enabled"] = not meta.get("schedule_enabled", False)
    write_device_meta(cfg.config_dir, filename, meta)
    import scheduler as _sched  # noqa: PLC0415
    _sched.sync_target(filename)
    logger.info("Schedule toggled for %s: enabled=%s%s", filename, meta["schedule_enabled"], _who(request))
    from git_versioning import commit_file  # noqa: PLC0415
    await commit_file(Path(cfg.config_dir), filename, "schedule toggle")
    return web.json_response({"ok": True, "schedule_enabled": meta["schedule_enabled"]})


@routes.post("/ui/api/targets/{filename}/schedule/once")
async def set_target_schedule_once(request: web.Request) -> web.Response:
    """Schedule a one-time upgrade at a specific date/time.

    Body: ``{"datetime": "2026-04-15T14:00:00Z"}``

    The scheduler fires the job when the datetime passes, then auto-clears
    the ``schedule_once`` field (no recurring schedule created).
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    path = safe_resolve(Path(cfg.config_dir), filename)
    if path is None or not path.exists():
        return json_error("Target not found", 404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    dt_str = body.get("datetime", "").strip()
    if not dt_str:
        return web.json_response({"error": "datetime required"}, status=400)

    # Validate it's a parseable ISO datetime.  Allow up to 60s in the past
    # so that "schedule for now" (immediate) doesn't get rejected due to
    # network/processing latency.
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td  # noqa: PLC0415
        parsed_dt = _dt.fromisoformat(dt_str)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=_tz.utc)
        if parsed_dt < _dt.now(_tz.utc) - _td(seconds=60):
            return web.json_response({"error": "Datetime must not be in the past"}, status=400)
    except ValueError:
        return web.json_response({"error": "Invalid datetime format (use ISO 8601)"}, status=400)

    meta = read_device_meta(cfg.config_dir, filename)
    meta["schedule_once"] = dt_str
    write_device_meta(cfg.config_dir, filename, meta)
    import scheduler as _sched  # noqa: PLC0415
    _sched.sync_target(filename)
    logger.info("One-time schedule set for %s at %s%s", filename, dt_str, _who(request))
    from git_versioning import commit_file  # noqa: PLC0415
    await commit_file(Path(cfg.config_dir), filename, "schedule once")
    return web.json_response({"ok": True, "schedule_once": dt_str})


@routes.post("/ui/api/compile")
async def start_compile(request: web.Request) -> web.Response:
    """Start a compile run.

    Body: {
        "targets": "all" | "outdated" | ["file.yaml", ...],
        "pinned_client_id": str | null,    # optional, pin to a specific worker
        "esphome_version": str | null,     # optional, override the global default per-job (#16)
    }
    Returns: { "run_id": "...", "enqueued": N }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    targets_param = body.get("targets", "all")
    pinned_client_id = body.get("pinned_client_id")  # optional: pin job to specific worker
    # Bug #97: optional per-job worker_tag_filter from the Upgrade
    # modal's "Tag expression" worker-selection radio. Same shape as a
    # routing-rule clause — ``{"op": "all_of"|"any_of"|"none_of",
    # "tags": [...]}``. Validated minimally here; the eligibility
    # builder accepts any well-shaped clause and skips malformed ones.
    worker_tag_filter_raw = body.get("worker_tag_filter")
    worker_tag_filter: dict | None = None
    if isinstance(worker_tag_filter_raw, dict):
        op = worker_tag_filter_raw.get("op")
        tags = worker_tag_filter_raw.get("tags")
        if op in ("all_of", "any_of", "none_of") and isinstance(tags, list):
            cleaned_tags = [str(t).strip() for t in tags if isinstance(t, str) and str(t).strip()]
            if cleaned_tags:
                worker_tag_filter = {"op": op, "tags": cleaned_tags}
    # #16: optional per-run ESPHome version override. Falls back to the global
    # default from set_esphome_version when not provided. We do NOT mutate the
    # global default — this is a per-job override only.
    version_override = body.get("esphome_version")
    # FD.2: compile-and-download mode. When true the worker runs
    # `esphome compile` (no OTA), POSTs the produced binary back, and
    # the user downloads it from the Queue tab. Mutually exclusive with
    # validate_only (which isn't exposed through this endpoint anyway).
    download_only = bool(body.get("download_only", False))
    # Bug #110: per-job override flag for routing rules. Set when the
    # user picked a Specific worker / Tag expression that conflicts
    # with active rules and confirmed the warning in the Upgrade modal.
    # The eligibility checks (BLOCKED-vs-PENDING re-eval and the
    # per-worker claim_next predicate) ignore routing rules for jobs
    # carrying this flag; the user's tag-filter / pin still applies.
    bypass_routing_rules = bool(body.get("bypass_routing_rules", False))
    # DM.3: optional per-job OTA address override from the
    # InstallToAddressModal. When set, must accompany a single-element
    # ``targets`` array (multi-target + address is meaningless and 400's
    # below). Goes through the same ``Job.ota_address`` plumbing the
    # rename auto-recompile already uses (#65 / ui_api.py:2795 region),
    # so no protocol change is needed.
    address_override_raw = body.get("address")
    address_override: str | None = None
    if address_override_raw is not None:
        if not isinstance(address_override_raw, str):
            return web.json_response(
                {"error": "address must be a string"}, status=400,
            )
        address_override = address_override_raw.strip()
        if not address_override:
            address_override = None
        elif len(address_override) > 253:
            # Same upper bound as a DNS hostname — anything longer can't
            # be a real target.
            return web.json_response(
                {"error": "address too long (max 253 chars)"}, status=400,
            )
    cfg = _cfg(request)
    queue = request.app["queue"]
    device_poller = request.app.get("device_poller")

    server_version = get_esphome_version()
    job_version = version_override or server_version
    all_targets = scan_configs(cfg.config_dir)

    if targets_param == "all":
        selected = all_targets
    elif targets_param == "outdated":
        # Select targets where running version != server version
        if device_poller:
            devices_by_target: dict[str, Device] = {
                dev.compile_target: dev
                for dev in device_poller.get_devices()
                if dev.compile_target
            }
        else:
            devices_by_target = {}

        selected = []
        for t in all_targets:
            dev = devices_by_target.get(t)
            if dev is None:
                # Unknown device state — include it to be safe
                selected.append(t)
            elif dev.running_version != server_version:
                selected.append(t)
    elif isinstance(targets_param, list):
        # Validate that specified targets exist
        valid = set(all_targets)
        selected = [t for t in targets_param if t in valid]
    else:
        return web.json_response({"error": "Invalid targets value"}, status=400)

    # DM.3: address override is single-target only (multi-target +
    # address makes no sense — every device gets the same OTA target,
    # which is wrong for any batch).
    if address_override and len(selected) != 1:
        return web.json_response(
            {"error": "address override requires exactly one target"},
            status=400,
        )

    # Build a map of target → device IP for OTA addressing
    # Bug #18 (1.6.1): resolve_ota_address picks a real IP over a
    # stale ``.local`` fallback so static-IP devices can't regress
    # to the mDNS hostname the worker's container can't resolve.
    ota_addresses: dict[str, str] = {}
    if device_poller:
        for dev in device_poller.get_devices():
            if dev.compile_target and dev.ip_address:
                addr = device_poller.resolve_ota_address(dev.name)
                if addr:
                    ota_addresses[dev.compile_target] = addr
    # DM.3: per-job address override wins over the auto-resolved value
    # for the single selected target. Stored on the job's ``ota_address``
    # field so the worker uses it as ``--device <addr>`` for the OTA pass.
    if address_override:
        ota_addresses[selected[0]] = address_override

    run_id = str(uuid.uuid4())
    enqueued = 0
    for target in selected:
        # VP.7: if the device is pinned to a specific version, use the pinned
        # version for this job — not the global/override version. This ensures
        # bulk "Upgrade All" doesn't accidentally flash pinned devices with
        # the wrong firmware. The version_override from the UI (when set via
        # the UpgradeModal) takes precedence over the pin, since the user
        # explicitly chose it for this specific run.
        effective_version = job_version
        if not version_override:
            device_meta = read_device_meta(cfg.config_dir, target)
            pinned = device_meta.get("pin_version")
            if pinned:
                effective_version = pinned

        # SOTA.3: auto-detect Thread targets. Thread devices use IPv6 mesh
        # only reachable from the HA host, so any OTA must be server-side.
        # Any worker can compile; the server performs the actual flash.
        _target_meta = get_device_metadata(cfg.config_dir, target)
        _is_thread = _target_meta.get("network_type") == "thread"
        effective_server_ota = _is_thread or bool(body.get("server_ota", False))

        from settings import get_settings as _gs  # noqa: PLC0415
        from git_versioning import get_head as _get_head  # noqa: PLC0415
        job = await queue.enqueue(
            target=target,
            esphome_version=effective_version,
            run_id=run_id,
            timeout_seconds=_gs().job_timeout,
            download_only=download_only,
            server_ota=effective_server_ota,
            ota_address=ota_addresses.get(target),
            pinned_client_id=pinned_client_id,
            config_hash=_get_head(Path(cfg.config_dir)),
            worker_tag_filter=worker_tag_filter,
            bypass_routing_rules=bypass_routing_rules,
        )
        if job is not None:
            # Bug 27: flag the job as triggered by a Home Assistant
            # service action when the caller authenticated with our
            # system-token Bearer (see ha_auth.py Path 2 —
            # ``ha_user.name == "esphome_fleet_integration"``).
            #
            # Bug #61: system-token bearer splits into *two* sources —
            # the HA integration's coordinator (HomeAssistant/* UA) vs
            # any other tool the user aimed at /ui/api/compile with the
            # same token (curl, scripts, Postman, etc.). Use User-Agent
            # to split: HomeAssistant/* → ha_action, anything else →
            # api_triggered. Both flags are mutually exclusive by
            # construction.
            ha_user = request.get("ha_user") or {}
            if ha_user.get("name") == "esphome_fleet_integration":
                user_agent = request.headers.get("User-Agent", "")
                if user_agent.startswith("HomeAssistant/"):
                    job.ha_action = True
                else:
                    job.api_triggered = True
            enqueued += 1

    logger.info(
        "Compile run %s: enqueued %d jobs (version=%s%s%s)%s",
        run_id, enqueued, job_version,
        " (override)" if version_override else "",
        f" pinned={pinned_client_id}" if pinned_client_id else "",
        _who(request),
    )
    # TG.3: a freshly-enqueued job whose target has rules with no
    # eligible online worker should land in BLOCKED, not PENDING.
    if enqueued > 0:
        from routing_eligibility import fire_and_forget  # noqa: PLC0415
        fire_and_forget(request.app)
    return web.json_response({"run_id": run_id, "enqueued": enqueued})


@routes.get("/ui/api/targets/{filename}/content")
async def get_target_content(request: web.Request) -> web.Response:
    """Return the raw YAML content of a config file."""
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    config_dir = Path(cfg.config_dir)
    path = safe_resolve(config_dir, filename)
    if path is None:
        return json_error("Invalid filename")
    if not path.exists():
        return json_error("File not found", 404)
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"content": content})


@routes.post("/ui/api/targets/{filename}/content")
async def save_target_content(request: web.Request) -> web.Response:
    """Write raw YAML content back to a config file.

    #53/#62: if the filename starts with ``.pending.``, the file is a staged
    new-device. On first save, write the content to the final ``<name>.yaml``
    (stripping the prefix) and delete the pending file. Returns
    ``{"ok": true, "renamed_to": "<name>.yaml"}``.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    config_dir = Path(cfg.config_dir)
    path = safe_resolve(config_dir, filename)
    if path is None:
        return json_error("Invalid filename")
    try:
        body = await request.json()
    except Exception:
        return json_error("Invalid JSON")
    content = body.get("content", "")
    # Bug #24: optional user-entered commit message. Passed through to
    # commit_file() which uses it instead of the auto-generated
    # "save: <file>" subject when present. Ignored when auto-commit is
    # off — the editor's "Save and Commit" button takes the separate
    # /files/{f}/commit path for that case.
    raw_msg = body.get("commit_message")
    commit_message = raw_msg.strip() if isinstance(raw_msg, str) and raw_msg.strip() else None

    from git_versioning import commit_file  # noqa: PLC0415

    is_staged = filename.startswith(_PENDING_PREFIX)
    if is_staged:
        final_name = filename[len(_PENDING_PREFIX):]
        final_path = safe_resolve(config_dir, final_name)
        if final_path is None:
            return json_error("Invalid filename")
        if final_path.exists():
            return json_error(f"{final_name} already exists")
        try:
            final_path.write_text(content, encoding="utf-8")
            path.unlink(missing_ok=True)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        logger.info("Saved staged %s → %s (%d bytes)", filename, final_name, len(content))
        await commit_file(Path(config_dir), final_name, "create", commit_message)
        return web.json_response({"ok": True, "renamed_to": final_name})

    try:
        path.write_text(content, encoding="utf-8")
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    # Invalidate config cache so changes are picked up immediately
    from scanner import _config_cache  # noqa: PLC0415
    _config_cache.pop(filename, None)
    logger.info("Saved %s (%d bytes)%s", filename, len(content), _who(request))
    await commit_file(Path(config_dir), filename, "save", commit_message)
    _broadcast_ws("targets_changed")
    return web.json_response({"ok": True})


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


_PENDING_PREFIX = ".pending."


def _ensure_config_dir(config_dir: Path) -> bool:
    """Lazy-create the config dir the first time a UI write needs it.

    Complements #86 (scanner stays silent on missing dir — no poll
    spam) — fixes #190 — a truly-empty install where the HA ESPHome
    builder add-on was never installed and the user clicks
    **Add Device**, the staged-file write would 500 with ``[Errno 2]
    No such file or directory`` because the parent dir doesn't exist.

    Creating is idempotent + logged once so operators see "first ever
    write landed" in the boot log, and a pre-existing dir is a no-op.
    Returns ``True`` if we actually created the dir so the caller can
    re-run ``init_repo`` (versioning=on + fresh dir → fresh repo).
    """
    if config_dir.exists():
        return False
    config_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Created %s on first UI write (#190)", config_dir)
    try:
        from settings import get_settings  # noqa: PLC0415
        if get_settings().versioning_enabled == "on":
            from git_versioning import init_repo  # noqa: PLC0415
            init_repo(config_dir)
    except Exception:
        logger.exception("Post-mkdir init_repo raised unexpectedly (#190)")
    return True


@routes.post("/ui/api/targets")
async def create_target(request: web.Request) -> web.Response:
    """Create a new device YAML file (CD.3).

    Body: ``{"filename": "<slug>", "source"?: "<existing.yaml>"}``

    - Without ``source``: creates a minimal stub YAML via ``create_stub_yaml``.
    - With ``source``: duplicates the source file and rewrites ``esphome.name``
      to the new filename via ``duplicate_device``.

    #53/#62: the file is written as ``.pending.<name>.yaml`` (a dotfile at the
    config root, invisible to the scanner which skips dotfiles). On first save,
    the save endpoint detects the ``.pending.`` prefix and renames to the final
    ``<name>.yaml``. If the user cancels, the #42 cleanup deletes the dotfile.

    Returns ``{"target": ".pending.<name>.yaml"}`` on success.
    """
    cfg = _cfg(request)
    config_dir = Path(cfg.config_dir)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    raw_name = str(body.get("filename", "")).strip()
    source = body.get("source")

    if not raw_name:
        return json_error("filename required")

    # Strip a ``.yaml`` extension if the caller included it, then validate
    # the slug portion.
    name = raw_name[:-5] if raw_name.lower().endswith(".yaml") else raw_name
    if not _SLUG_RE.match(name):
        return json_error(
            "filename must be lowercase, start with a letter or digit, and "
            "contain only letters, digits, and hyphens",
        )
    if len(name) > 64:
        return json_error("filename too long (max 64 characters)")

    new_filename = f"{name}.yaml"
    # #190: first-install path — the user may be creating the very
    # first device on a box that has no ``/config/esphome/`` yet
    # (HAOS without the ESPHome builder add-on, standalone Docker
    # without a pre-mounted config dir). Create the dir now and
    # fire init_repo if versioning is on.
    _ensure_config_dir(config_dir)
    # Check for collision with the FINAL name (not the staging name)
    final_dest = safe_resolve(config_dir, new_filename)
    if final_dest is None:
        return json_error("Invalid filename")
    if final_dest.exists():
        return json_error(f"{new_filename} already exists")

    if source:
        src_name = str(source).strip()
        src_path = safe_resolve(config_dir, src_name)
        if src_path is None or not src_path.exists():
            return json_error("Source file not found", 404)
        try:
            yaml_text = duplicate_device(str(config_dir), src_name, name)
        except FileNotFoundError:
            return json_error("Source file not found", 404)
        except ValueError as e:
            return json_error(f"Source invalid: {e}")
    else:
        yaml_text = create_stub_yaml(name)

    # Write as a dotfile so the scanner doesn't pick it up
    pending_filename = f"{_PENDING_PREFIX}{new_filename}"
    staged_path = config_dir / pending_filename
    try:
        staged_path.write_text(yaml_text, encoding="utf-8")
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    staged_target = pending_filename
    logger.info("Created staged target %s (source=%s, %d bytes)", staged_target, source or "stub", len(yaml_text))
    return web.json_response({"target": staged_target, "ok": True})


@routes.delete("/ui/api/targets/{filename}")
async def delete_target(request: web.Request) -> web.Response:
    """Delete (or archive) a YAML config file and cancel any pending jobs for it."""
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    config_dir = Path(cfg.config_dir)
    path = safe_resolve(config_dir, filename)
    if path is None:
        return json_error("Invalid filename")

    if not path.exists():
        return json_error("File not found", 404)

    archive = request.rel_url.query.get("archive", "true") == "true"

    try:
        if archive:
            # Bug #63: archive-with-git-mv preserves rename history across
            # the soft-delete boundary. When the file is tracked, a single
            # commit shows as ``R original → .archive/original``; `git log
            # --follow .archive/original` threads back through the device's
            # entire pre-archive history. Falls back to a raw rename if the
            # file was never committed.
            from git_versioning import archive_and_commit  # noqa: PLC0415
            ok = await archive_and_commit(config_dir, filename)
            if not ok:
                return web.json_response({"error": "archive failed"}, status=500)
        else:
            path.unlink()
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    # Cancel any non-terminal jobs for this target. PENDING is the obvious
    # case; BLOCKED jobs would otherwise stick around forever pointing at a
    # deleted target; WORKING jobs may still be compiling on a worker and
    # the cancel won't actually stop the in-flight subprocess (the worker
    # will surface a "job in unexpected state CANCELLED" warning when it
    # reports back), but at least the queue state is correct after delete.
    queue = request.app["queue"]
    job_ids = [
        j.id for j in queue.get_all()
        if j.target == filename
        and j.state in (JobState.PENDING, JobState.BLOCKED, JobState.WORKING)
    ]
    if job_ids:
        await queue.cancel(job_ids)

    # Invalidate config cache for the deleted file
    from scanner import _config_cache  # noqa: PLC0415
    _config_cache.pop(filename, None)

    # DM.1: evict the just-archived device from the poller so the UI's
    # archived row freezes at last_seen=now rather than staying "online"
    # for ~4 h until the TTL prune fires. Mirrors the eviction in
    # ``rename_target`` above. Skipped on permanent-delete because the
    # row vanishes from the table entirely in that case.
    if archive:
        device_poller = request.app.get("device_poller")
        if device_poller:
            stale_name = None
            for d in device_poller.get_devices():
                if d.compile_target == filename:
                    stale_name = d.name
                    break
            if stale_name and stale_name in device_poller._devices:
                del device_poller._devices[stale_name]
                logger.debug("Evicted device %s after archive of %s", stale_name, filename)

    logger.info("Deleted config %s (archive=%s)%s", filename, archive, _who(request))
    if not archive:
        # Permanent-delete path still uses commit_file to record the
        # raw deletion — archive_and_commit above already committed
        # the rename case.
        from git_versioning import commit_file  # noqa: PLC0415
        await commit_file(config_dir, filename, "delete")
    _broadcast_ws("targets_changed")
    return web.json_response({"ok": True})


@routes.get("/ui/api/archive")
async def list_archive(request: web.Request) -> web.Response:
    """List archived YAML config files."""
    cfg = _cfg(request)
    archive_dir = Path(cfg.config_dir) / ".archive"
    if not archive_dir.exists():
        return web.json_response([])
    files = []
    for f in sorted(archive_dir.iterdir()):
        if f.suffix in (".yaml", ".yml") and f.is_file():
            files.append({
                "filename": f.name,
                "size": f.stat().st_size,
                "archived_at": f.stat().st_mtime,
            })
    return web.json_response(files)


@routes.post("/ui/api/archive/{filename}/restore")
async def restore_archive(request: web.Request) -> web.Response:
    """Restore an archived config file back to the config directory."""
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    config_dir = Path(cfg.config_dir)
    archive_dir = config_dir / ".archive"
    src = safe_resolve(archive_dir, filename)
    if src is None:
        return json_error("Invalid filename")

    if not src.exists():
        return json_error("Archived file not found", 404)

    dest = config_dir / filename
    if dest.exists():
        return web.json_response({"error": f"{filename} already exists in config directory"}, status=409)

    # Bug #63: git-mv restore threads the file's history across the
    # archive boundary. Falls back to raw rename when the archived
    # file isn't git-tracked (pre-#63 archive that predates the move
    # to un-ignored .archive/).
    from git_versioning import restore_and_commit  # noqa: PLC0415
    ok = await restore_and_commit(config_dir, filename)
    if not ok:
        return web.json_response({"error": "restore failed"}, status=500)

    logger.info("Restored config %s from archive", filename)
    return web.json_response({"ok": True})


@routes.delete("/ui/api/archive/{filename}")
async def delete_archived(request: web.Request) -> web.Response:
    """Permanently delete an archived config file.

    #94: routes through :func:`git_versioning.delete_archived_and_commit`
    so the ``git rm`` lands in history when ``.archive/`` is tracked.
    Pre-#94 this was a bare ``os.unlink`` that left a dangling
    ``deleted:`` entry in the working tree until the next auto-commit
    ran for some unrelated write.
    """
    filename = request.match_info["filename"]
    cfg = _cfg(request)
    config_dir = Path(cfg.config_dir)
    archive_dir = config_dir / ".archive"
    path = safe_resolve(archive_dir, filename)
    if path is None:
        return json_error("Invalid filename")

    if not path.exists():
        return json_error("File not found", 404)

    from git_versioning import delete_archived_and_commit  # noqa: PLC0415
    try:
        ok = await delete_archived_and_commit(config_dir, filename)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    if not ok:
        return web.json_response({"error": "delete failed"}, status=500)

    logger.info("Deleted archived config %s", filename)
    return web.json_response({"ok": True})


@routes.post("/ui/api/targets/{filename}/rename")
async def rename_target(request: web.Request) -> web.Response:
    """Rename a YAML config file and update the esphome.name field within it."""
    filename = request.match_info["filename"]
    try:
        body = await request.json()
    except Exception:
        return json_error("Invalid JSON")

    new_name = body.get("new_name", "").strip()
    if not new_name:
        return web.json_response({"error": "new_name required"}, status=400)

    cfg = _cfg(request)
    config_dir = Path(cfg.config_dir)
    old_path = safe_resolve(config_dir, filename)
    if old_path is None:
        return json_error("Invalid filename")

    if not old_path.exists():
        return json_error("File not found", 404)

    # Derive new filename: lowercase, spaces → hyphens, ensure .yaml extension
    new_filename = new_name.replace(" ", "-").lower()
    if not new_filename.endswith(".yaml"):
        new_filename += ".yaml"

    new_path = safe_resolve(config_dir, new_filename)
    if new_path is None:
        return json_error("Invalid new_name")

    if new_path.exists() and new_path != old_path:
        return web.json_response({"error": f"{new_filename} already exists"}, status=409)

    try:
        content = old_path.read_text(encoding="utf-8")
        # PY-1: parse YAML to identify the right binding (substitutions.name
        # preferred, esphome.name as fallback) and the OLD value, then do a
        # literal-value rewrite on that single line. Comments are preserved;
        # ${...} indirection is handled correctly. The previous regex misfired
        # on substitutions, on quoted-with-trailing-comment values, and on
        # configs where esphome: wasn't the first top-level block.
        from scanner import rename_device_in_yaml  # noqa: PLC0415
        base_name = new_filename.replace(".yaml", "")
        new_content, rewritten = rename_device_in_yaml(content, base_name)
        new_path.write_text(new_content, encoding="utf-8")
        if new_path != old_path:
            old_path.unlink()
        if not rewritten:
            logger.warning(
                "rename_target: %s → %s renamed on disk but the YAML's "
                "internal name binding wasn't safely rewriteable "
                "(no substitutions.name, no literal esphome.name, or "
                "an unresolvable ${...} reference) — user must edit it manually",
                filename, new_filename,
            )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    # Invalidate config cache and force device poller to rescan
    from scanner import _config_cache, scan_configs, build_name_to_target_map  # noqa: PLC0415
    _config_cache.pop(filename, None)
    _config_cache.pop(new_filename, None)

    # Capture OTA address and remove stale device entry for the old filename.
    # Must happen before rescanning so we can still find the device by old compile_target.
    # After rename, the old mDNS device name no longer maps to any target and
    # would show up as an unmanaged device until mDNS re-discovers the new name.
    device_poller = request.app.get("device_poller")
    old_device_addr = None
    if device_poller:
        old_dev_name = None
        for d in device_poller.get_devices():
            if d.compile_target == filename:
                old_dev_name = d.name
                # Bug #18 (1.6.1): route through the shared best-address
                # helper so rename compiles get the same static-IP-aware
                # resolution as every other OTA path.
                old_device_addr = device_poller.resolve_ota_address(d.name)
                break
        if old_dev_name and old_dev_name in device_poller._devices:
            del device_poller._devices[old_dev_name]
            logger.debug("Removed stale device entry %s after rename to %s", old_dev_name, new_filename)

    # Force immediate rescan so the UI shows the new name right away
    if device_poller:
        cfg = _cfg(request)
        targets = scan_configs(cfg.config_dir)
        name_map, enc_keys, addr_overrides, addr_sources = build_name_to_target_map(cfg.config_dir, targets)
        device_poller.update_compile_targets(targets, name_map, enc_keys, addr_overrides, addr_sources)

    logger.info("Renamed config %s → %s%s", filename, new_filename, _who(request))
    _broadcast_ws("targets_changed")

    queue = request.app["queue"]
    server_version = get_esphome_version()
    from settings import get_settings as _gs  # noqa: PLC0415
    from git_versioning import get_head as _get_head  # noqa: PLC0415
    # SOTA.3: detect Thread target for rename-triggered recompile.
    _rename_meta = get_device_metadata(_cfg(request).config_dir, new_filename)
    _rename_server_ota = _rename_meta.get("network_type") == "thread"
    await queue.enqueue(
        target=new_filename,
        esphome_version=server_version,
        run_id=str(uuid.uuid4()),
        timeout_seconds=_gs().job_timeout,
        server_ota=_rename_server_ota,
        ota_address=old_device_addr,
        config_hash=_get_head(config_dir),
    )
    logger.info("Enqueued compile+OTA for renamed device %s", new_filename)
    # TG.3: re-eval the just-enqueued job against current rules.
    from routing_eligibility import fire_and_forget  # noqa: PLC0415
    fire_and_forget(request.app)

    # AV.2: commit both paths so the rename shows up as a
    # delete-of-old + add-of-new. `git add --all -- <path>` picks up
    # the missing-file state for the old path.
    from git_versioning import commit_file  # noqa: PLC0415
    if new_filename != filename:
        await commit_file(config_dir, filename, "rename (old)")
    await commit_file(config_dir, new_filename, "rename")
    return web.json_response({"ok": True, "new_filename": new_filename})


@routes.get("/ui/api/targets/{filename}/api-key")
async def get_api_key(request: web.Request) -> web.Response:
    """Return the ESPHome API encryption key for a target device."""
    filename = request.match_info["filename"]
    device_poller = request.app.get("device_poller")
    if device_poller:
        for name, key in device_poller._encryption_keys.items():
            target = device_poller._map_target(name)
            if target == filename:
                return web.json_response({"key": key})
    return web.json_response({"error": "No API key found"}, status=404)


@routes.post("/ui/api/targets/{filename}/restart")
async def restart_device(request: web.Request) -> web.Response:
    """Restart an ESPHome device via the native API (preferred) or HA button entity (fallback).

    Bug #12: previously the HA fallback called ``button.press`` with a guessed
    entity_id and reported success on HTTP 200 — but HA's button.press service
    returns 200 even for non-existent entities, so a wrong guess silently
    no-op'd. Now:

    1. Native API path is the primary route. Failures are logged at WARNING
       (was DEBUG, so operators couldn't see why it fell through).
    2. HA fallback verifies the entity_id actually exists (GET /states/<id>
       returns 404 for missing entities) before calling button.press.
    3. Multiple entity_id candidates are tried, derived from filename,
       device_name_raw, friendly_name, and the cached HA entity registry.
    4. If no candidate works, the response is a real error with the list of
       candidates that were tried, not a fake "ok".
    """
    import asyncio as _asyncio  # noqa: PLC0415
    import os  # noqa: PLC0415
    import aioesphomeapi as _api  # noqa: PLC0415

    filename = request.match_info["filename"]
    device_poller = request.app.get("device_poller")

    # ------------------------------------------------------------------
    # 1. Native API path — works without HA integration. Connects directly
    #    to the device, lists entities, finds the restart button, presses it.
    # ------------------------------------------------------------------
    native_error: "str | None" = None
    if device_poller:
        dev = None
        for d in device_poller.get_devices():
            if d.compile_target == filename:
                dev = d
                break
        if dev and dev.ip_address:
            noise_psk = device_poller._encryption_keys.get(dev.name)
            addr = device_poller._address_overrides.get(dev.name) or dev.ip_address
            try:
                client = _api.APIClient(addr, 6053, password=None, noise_psk=noise_psk)
                await client.connect(login=True)
                try:
                    entities = await client.list_entities_services()
                    # entities is a tuple: (entities_list, services_list)
                    restart_entity = None
                    for entity in entities[0]:
                        obj_id = getattr(entity, "object_id", "") or ""
                        if "restart" in obj_id.lower() and hasattr(entity, "key"):
                            restart_entity = entity
                            break
                    if restart_entity is not None:
                        client.button_command(restart_entity.key)
                        # Give the protocol a beat to flush before disconnect.
                        # button_command writes to the socket synchronously but
                        # the bytes need to leave the buffer; without this brief
                        # wait the disconnect can race the write.
                        await _asyncio.sleep(0.1)
                        logger.info(
                            "Restarted %s via native API (object_id=%s, key=%d)",
                            filename, getattr(restart_entity, "object_id", "?"), restart_entity.key,
                        )
                        return web.json_response({"ok": True, "method": "native_api"})
                    native_error = "device exposes no restart button entity"
                    logger.warning(
                        "Native API restart for %s: %s — falling back to HA",
                        filename, native_error,
                    )
                finally:
                    await client.disconnect()
            except Exception as exc:
                native_error = str(exc)
                logger.warning(
                    "Native API restart failed for %s: %s — falling back to HA",
                    filename, native_error,
                )
        elif dev is None:
            native_error = "device not found in poller"
        else:
            native_error = "device has no known IP address"

    # ------------------------------------------------------------------
    # 2. HA REST API fallback. Build a list of entity_id candidates and
    #    verify each one exists in HA before pressing.
    # ------------------------------------------------------------------
    meta = get_device_metadata(_cfg(request).config_dir, filename)
    friendly = meta.get("friendly_name")
    raw_name: str = meta.get("device_name_raw") or filename.replace(".yaml", "")
    file_stem = filename.replace(".yaml", "")

    candidate_names: list[str] = []
    for n in (friendly, raw_name, file_stem):
        if n:
            norm = _normalize_for_ha(n)
            if norm and norm not in candidate_names:
                candidate_names.append(norm)
    candidate_entity_ids = [f"button.{n}_restart" for n in candidate_names]

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        # SI (WORKITEMS-1.6.2): 503 Service Unavailable, not 500.
        # Native-API restart failed (device offline or no button) AND
        # the HA-service fallback is unreachable because we're running
        # standalone — a "feature unavailable" situation, not a server
        # error. 503 signals that distinction to the UI + any tooling
        # that treats 5xx as "server broken." Body is unchanged so
        # operators still see the specific reason in the JSON.
        return web.json_response(
            {
                "error": "Could not restart device",
                "native_api_error": native_error,
                "ha_fallback_error": "no SUPERVISOR_TOKEN",
                "hint": (
                    "HA-service fallback requires Home Assistant. "
                    "Running in standalone mode; restart via the device's "
                    "physical button or Web UI."
                ),
                "candidates_tried": [],
            },
            status=503,
        )

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {token}"}
            tried: list[str] = []
            for entity_id in candidate_entity_ids:
                tried.append(entity_id)
                # Verify the entity exists first — HA's button.press returns
                # 200 even for missing entities, which is why bug #12 went
                # unnoticed.
                async with session.get(
                    f"http://supervisor/core/api/states/{entity_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as state_resp:
                    if state_resp.status != 200:
                        logger.debug(
                            "Restart candidate %s does not exist in HA (HTTP %d)",
                            entity_id, state_resp.status,
                        )
                        continue
                # Entity exists — press the button.
                async with session.post(
                    "http://supervisor/core/api/services/button/press",
                    headers=headers,
                    json={"entity_id": entity_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        logger.info("Restarted device %s via HA (%s)", filename, entity_id)
                        return web.json_response(
                            {"ok": True, "method": "ha_api", "entity_id": entity_id},
                        )
                    body = await resp.text()
                    logger.warning(
                        "HA button.press failed for %s: HTTP %d — %s",
                        entity_id, resp.status, body,
                    )
            # No candidate worked.
            logger.warning(
                "Restart failed for %s: native_api=%s, no HA candidate matched (tried %s)",
                filename, native_error or "skipped", tried,
            )
            return web.json_response(
                {
                    "error": "Could not restart device — no native API restart button and no matching HA entity",
                    "native_api_error": native_error,
                    "candidates_tried": tried,
                },
                status=404,
            )
    except Exception as exc:
        logger.warning("Restart failed for %s: %s", filename, exc)
        return web.json_response(
            {
                "error": str(exc),
                "native_api_error": native_error,
                "candidates_tried": candidate_entity_ids,
            },
            status=500,
        )


@routes.post("/ui/api/targets/{filename}/ping")
async def ping_device(request: web.Request) -> web.Response:
    """DM.2: ICMP ping a device and return RTT / loss stats.

    Resolves the target's address through ``device_poller.resolve_ota_address``
    so the ping target matches what an OTA upload would hit (real IP from
    mDNS / static-IP override beats a stale ``.local`` hostname per #18).
    Runs ``icmplib.async_ping(host, count=10, interval=0.2, timeout=2,
    privileged=False)`` — unprivileged ICMP via ``net.ipv4.ping_group_range``,
    which the HA add-on container and the standalone Docker container both
    inherit. No new dependencies (icmplib already pinned for the
    device_poller's ping fallback). Worst-case wall time for an unreachable
    host is `(count-1)*interval + timeout` ≈ 3.8 s — batch UX, no streaming.

    Returns: flat dict with ``is_alive``, ``packets_sent``, ``packets_received``,
    ``packet_loss``, ``min_rtt``, ``avg_rtt``, ``max_rtt``, ``jitter``, plus
    ``target``, ``address``, ``ran_at``. Errors:
      - 404 ``no_resolved_address`` — poller has no address for this device
        (ESPHome device never came up on mDNS, no ``use_address`` override).
      - 404 ``unknown_target`` — the YAML filename doesn't match any device
        the poller has seen.
    """
    import time as _time  # noqa: PLC0415

    filename = request.match_info["filename"]
    device_poller = request.app.get("device_poller")
    if device_poller is None:
        return web.json_response(
            {"error": "device poller unavailable"}, status=503,
        )

    # Find the device that compiles to this YAML — same lookup pattern as
    # the restart endpoint above. We look up by ``compile_target`` so the
    # filename-to-device mapping is authoritative against the live poll.
    dev = None
    for d in device_poller.get_devices():
        if d.compile_target == filename:
            dev = d
            break
    if dev is None:
        return web.json_response(
            {"error": "unknown_target", "target": filename}, status=404,
        )

    address = device_poller.resolve_ota_address(dev.name)
    if not address:
        return web.json_response(
            {
                "error": "no_resolved_address",
                "target": filename,
                "device_name": dev.name,
            },
            status=404,
        )

    logger.info(
        "Ping: target=%s name=%s address=%s (count=10, interval=0.2s, timeout=2s)",
        filename, dev.name, address,
    )

    # #206: try unprivileged ICMP first (Linux hosts where
    # ``net.ipv4.ping_group_range`` allows it), fall back to a raw-socket
    # ping (HA addon container with ``NET_RAW`` capability granted via
    # ``ha-addon/config.yaml``). HAOS ships ``ping_group_range = 1 0``
    # (empty) so unprivileged ICMP fails with ``SocketPermissionError``;
    # without the fallback the modal would always immediately fail there.
    try:
        from icmplib import SocketPermissionError, async_ping  # noqa: PLC0415
        try:
            host = await async_ping(
                address, count=10, interval=0.2, timeout=2, privileged=False,
            )
        except SocketPermissionError:
            host = await async_ping(
                address, count=10, interval=0.2, timeout=2, privileged=True,
            )
    except Exception as exc:
        logger.warning("Ping %s (%s) failed: %s", filename, address, exc)
        return web.json_response(
            {"error": "ping_failed", "detail": str(exc), "target": filename, "address": address},
            status=500,
        )

    return web.json_response({
        "target": filename,
        "address": address,
        "ran_at": _time.time(),
        "is_alive": bool(host.is_alive),
        "packets_sent": int(host.packets_sent),
        "packets_received": int(host.packets_received),
        "packet_loss": float(host.packet_loss),
        "min_rtt": float(host.min_rtt),
        "avg_rtt": float(host.avg_rtt),
        "max_rtt": float(host.max_rtt),
        "jitter": float(host.jitter),
    })


@routes.post("/ui/api/retry")
async def retry_jobs(request: web.Request) -> web.Response:
    """Re-enqueue failed/timed_out jobs.

    Body: { "job_ids": ["uuid", ...] | "all_failed" }
    Returns: { "retried": N }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    job_ids_param = body.get("job_ids", [])
    queue = request.app["queue"]
    cfg = _cfg(request)

    server_version = get_esphome_version()

    if job_ids_param == "all_failed":
        job_ids = [
            j.id for j in queue.get_all()
            if j.state in (JobState.FAILED, JobState.TIMED_OUT)
            or (j.state == JobState.SUCCESS and j.ota_result == "failed")
        ]
    elif isinstance(job_ids_param, list):
        job_ids = job_ids_param
    else:
        return web.json_response({"error": "job_ids must be a list or 'all_failed'"}, status=400)

    # #51: build a per-target version map that respects device pins.
    # If a device is pinned to a specific ESPHome version, the retry should
    # use that version — not blindly use the server default.
    target_versions: dict[str, str] = {}
    for jid in job_ids:
        job = queue._jobs.get(jid)
        if job is None:
            continue
        if job.target not in target_versions:
            meta = read_device_meta(cfg.config_dir, job.target)
            pinned = meta.get("pin_version")
            target_versions[job.target] = pinned if pinned else server_version

    from settings import get_settings as _gs_retry  # noqa: PLC0415
    from git_versioning import get_head as _get_head_retry  # noqa: PLC0415
    new_jobs = await queue.retry(
        job_ids, server_version, str(uuid.uuid4()), _gs_retry().job_timeout,
        target_versions=target_versions,
        config_hash=_get_head_retry(Path(cfg.config_dir)),
    )
    return web.json_response({"retried": len(new_jobs)})


async def _remove_worker_handler(request: web.Request, client_id: str) -> web.Response:
    """Remove an offline worker from the registry."""
    registry = request.app["registry"]
    from settings import get_settings as _gs  # noqa: PLC0415

    if registry.is_online(client_id, _gs().worker_offline_threshold):
        return web.json_response({"error": "Cannot remove an online worker"}, status=409)
    if not registry.remove(client_id):
        return web.json_response({"error": "Unknown client_id"}, status=404)
    return web.json_response({"ok": True})


async def _set_disabled_handler(request: web.Request, client_id: str) -> web.Response:
    """Enable or disable a worker."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    disabled = bool(body.get("disabled", True))
    registry = request.app["registry"]
    if not registry.set_disabled(client_id, disabled):
        return web.json_response({"error": "Unknown client_id"}, status=404)
    return web.json_response({"ok": True, "disabled": disabled})


# New worker routes

@routes.delete("/ui/api/workers/{client_id}")
async def remove_worker(request: web.Request) -> web.Response:
    """Remove an offline worker from the registry."""
    return await _remove_worker_handler(request, request.match_info["client_id"])


@routes.post("/ui/api/workers/{client_id}/disable")
async def set_worker_disabled(request: web.Request) -> web.Response:
    """Enable or disable a worker."""
    return await _set_disabled_handler(request, request.match_info["client_id"])


@routes.post("/ui/api/workers/{client_id}/parallel-jobs")
async def set_worker_parallel_jobs(request: web.Request) -> web.Response:
    """Set the requested max_parallel_jobs for a worker. Pushed via next heartbeat."""
    client_id = request.match_info["client_id"]
    registry = request.app["registry"]
    worker = registry.get(client_id)
    if not worker:
        return web.json_response({"error": "Worker not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    value = body.get("max_parallel_jobs")
    if not isinstance(value, int) or value < 0 or value > 32:
        return web.json_response({"error": "max_parallel_jobs must be 0-32"}, status=400)
    worker.requested_max_parallel_jobs = value
    logger.info("Worker %s (%s): requested max_parallel_jobs set to %d", client_id, worker.hostname, value)
    # Persist local worker slot count across restarts
    if worker.hostname == "local-worker":
        try:
            Path("/data/local_worker_slots").write_text(str(value))
        except Exception:
            pass
    _broadcast_ws("workers_changed")
    return web.json_response({"ok": True, "max_parallel_jobs": value})


@routes.post("/ui/api/workers/{client_id}/disk-quota")
async def set_worker_disk_quota(request: web.Request) -> web.Response:
    """DQ.5 — set or clear a worker's disk-quota override.

    Body ``{"disk_quota_bytes": <int> | null}``. Null clears the override
    so the worker inherits ``AppSettings.default_worker_disk_quota_bytes``.
    The next heartbeat (≤10s) carries the new effective value to the worker
    via ``HeartbeatResponse.set_disk_quota_bytes`` — no restart required.
    """
    client_id = request.match_info["client_id"]
    registry = request.app["registry"]
    worker = registry.get(client_id)
    if not worker:
        return web.json_response({"error": "Worker not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    value = body.get("disk_quota_bytes")
    if value is not None:
        if not isinstance(value, int) or isinstance(value, bool):
            return web.json_response(
                {"error": "disk_quota_bytes must be an integer or null"},
                status=400,
            )
        # Same floor/ceiling/granularity as the fleet default validator
        # (settings.py). Whole-GiB multiples keep the UI's
        # `Math.round(bytes / GiB)` display honest — see the validator
        # comment for the silent-rewrite scenario.
        if value < 1 * 1024 ** 3 or value > 1024 * 1024 ** 3:
            return web.json_response(
                {"error": "disk_quota_bytes must be between 1 GiB and 1 TiB"},
                status=400,
            )
        if value % (1024 ** 3) != 0:
            return web.json_response(
                {"error": "disk_quota_bytes must be a whole-GiB multiple"},
                status=400,
            )
    quota_store = request.app.get("worker_disk_quota_store")
    if quota_store is not None:
        identity = worker.hostname or worker.client_id
        quota_store.set_quota(identity, value)
    registry.set_disk_quota(client_id, value)
    logger.info(
        "Worker %s (%s): disk_quota_bytes set to %r",
        client_id, worker.hostname, value,
    )
    _broadcast_ws("workers_changed")
    return web.json_response({"ok": True, "disk_quota_bytes": value})


@routes.post("/ui/api/workers/{client_id}/clean")
async def clean_worker_cache(request: web.Request) -> web.Response:
    """Request a worker to clean its build cache. Pushed via next heartbeat."""
    client_id = request.match_info["client_id"]
    registry = request.app["registry"]
    worker = registry.get(client_id)
    if not worker:
        return web.json_response({"error": "Worker not found"}, status=404)
    worker.pending_clean = True
    logger.info("Worker %s (%s): clean build cache requested", client_id, worker.hostname)
    _broadcast_ws("workers_changed")
    return web.json_response({"ok": True})


@routes.post("/ui/api/workers/{client_id}/tags")
async def set_worker_tags(request: web.Request) -> web.Response:
    """TG.4: authoritative worker-tag edit from the UI.

    Body: ``{"tags": ["a", "b"]}`` — a JSON array of strings (the wire shape
    the routing-rules engine works with). The store normalises (trim / drop
    empties / dedupe, case-sensitive, preserves first-seen order). Persists
    to ``/data/worker-tags.json`` and updates the in-memory registry record
    so the next ``/ui/api/workers`` poll surfaces the change.
    """
    client_id = request.match_info["client_id"]
    registry = request.app["registry"]
    worker = registry.get(client_id)
    if not worker:
        return web.json_response({"error": "Worker not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Expected a JSON object"}, status=400)
    raw_tags = body.get("tags", [])
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        return web.json_response({"error": "tags must be an array of strings"}, status=400)

    tag_store = request.app.get("worker_tag_store")
    identity = worker.hostname or client_id
    if tag_store is not None:
        normalised = tag_store.set_tags(identity, raw_tags)
    else:
        # Test rigs without a store: normalise inline so behaviour matches.
        from worker_tags import _normalise  # noqa: PLC0415
        normalised = _normalise(raw_tags)
    registry.set_tags(client_id, normalised)
    logger.info(
        "Worker %s (%s): tags set to %r%s",
        client_id, worker.hostname, normalised, _who(request),
    )
    _broadcast_ws("workers_changed")
    # TG.3: tag change may unblock or newly-block jobs.
    from routing_eligibility import fire_and_forget  # noqa: PLC0415
    fire_and_forget(request.app)
    return web.json_response({"ok": True, "tags": normalised})


# ---------------------------------------------------------------------------
# TG.4 — routing-rules CRUD. Global rule list lives in /data/routing-rules.json
# (RoutingRuleStore); per-device additive rules ride along in the YAML
# metadata comment block via the existing ``meta`` endpoint (TG.5
# round-trip is verified by tests/test_scanner.py).
# ---------------------------------------------------------------------------


def _rule_to_dict_handler(request: web.Request, rule_obj):  # type: ignore[no-untyped-def]
    """Serialise a Rule for the wire — re-uses routing._rule_to_dict but
    keeps the import local so a bare ``import routing`` in this module
    doesn't pull the dataclass model into every UI handler that doesn't
    need it."""
    from routing import _rule_to_dict  # noqa: PLC0415
    return _rule_to_dict(rule_obj)


def _slugify(name: str) -> str:
    """Auto-generate a URL-safe rule id from ``name``. Lowercase, ASCII
    alphanum + dashes, collapsed runs, trimmed leading/trailing dashes.
    Empty input → empty string (caller validates)."""
    out: list[str] = []
    prev_dash = False
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif ch in (" ", "-", "_"):
            if not prev_dash:
                out.append("-")
                prev_dash = True
    s = "".join(out).strip("-")
    return s


def _parse_clauses(raw: object, side_name: str) -> list:
    """Convert the wire shape (list of dicts) into a list of Clause."""
    from routing import RoutingRuleError, _clause_from_dict  # noqa: PLC0415
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RoutingRuleError(f"{side_name} must be a list of clauses")
    return [_clause_from_dict(c if isinstance(c, dict) else {}) for c in raw]


def _parse_rule(body: dict, *, default_id: str | None = None):  # type: ignore[no-untyped-def]
    """Build a Rule from a JSON body, auto-slugging ``id`` from ``name``
    when absent. Raises RoutingRuleError on shape problems."""
    from routing import Rule, RoutingRuleError  # noqa: PLC0415
    name = str(body.get("name") or "").strip()
    if not name:
        raise RoutingRuleError("rule 'name' is required")
    rule_id = str(body.get("id") or default_id or _slugify(name))
    if not rule_id:
        raise RoutingRuleError("rule 'id' could not be derived from 'name' — provide one explicitly")
    severity = body.get("severity") or "required"
    if severity != "required":
        # Defensive — RoutingRuleStore validates this too, but catching it
        # here gives the same shape error every API field gets.
        raise RoutingRuleError(
            f"severity must be 'required' (got {severity!r}); "
            "preferred-with-weight is reserved for a future release",
        )
    return Rule(
        id=rule_id,
        name=name,
        severity="required",  # narrowed by the check above; satisfies mypy
        device_match=_parse_clauses(body.get("device_match"), "device_match"),
        worker_match=_parse_clauses(body.get("worker_match"), "worker_match"),
    )


@routes.get("/ui/api/routing-rules")
async def list_routing_rules(request: web.Request) -> web.Response:
    store = request.app.get("routing_rule_store")
    if store is None:
        return web.json_response({"rules": []})
    rules = [_rule_to_dict_handler(request, r) for r in store.list_rules()]
    return web.json_response({"rules": rules})


@routes.post("/ui/api/routing-rules")
async def create_routing_rule(request: web.Request) -> web.Response:
    store = request.app.get("routing_rule_store")
    if store is None:
        return web.json_response({"error": "Routing not configured"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Expected a JSON object"}, status=400)
    from routing import RoutingRuleError  # noqa: PLC0415
    try:
        rule = _parse_rule(body)
        created = store.create_rule(rule)
    except RoutingRuleError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    logger.info("Created routing rule %s (%s)%s", created.id, created.name, _who(request))
    _broadcast_ws("routing_rules_changed")
    # TG.3: a new rule may push PENDING jobs to BLOCKED.
    from routing_eligibility import fire_and_forget  # noqa: PLC0415
    fire_and_forget(request.app)
    return web.json_response(_rule_to_dict_handler(request, created), status=201)


@routes.put("/ui/api/routing-rules/{rule_id}")
async def update_routing_rule(request: web.Request) -> web.Response:
    store = request.app.get("routing_rule_store")
    if store is None:
        return web.json_response({"error": "Routing not configured"}, status=503)
    rule_id = request.match_info["rule_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Expected a JSON object"}, status=400)
    from routing import RoutingRuleError  # noqa: PLC0415
    try:
        rule = _parse_rule(body, default_id=rule_id)
        updated = store.update_rule(rule_id, rule)
    except RoutingRuleError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    logger.info("Updated routing rule %s (%s)%s", updated.id, updated.name, _who(request))
    _broadcast_ws("routing_rules_changed")
    # TG.3: rule edit may shift jobs PENDING ↔ BLOCKED in either direction.
    from routing_eligibility import fire_and_forget  # noqa: PLC0415
    fire_and_forget(request.app)
    return web.json_response(_rule_to_dict_handler(request, updated))


@routes.delete("/ui/api/routing-rules/{rule_id}")
async def delete_routing_rule(request: web.Request) -> web.Response:
    store = request.app.get("routing_rule_store")
    if store is None:
        return web.json_response({"error": "Routing not configured"}, status=503)
    rule_id = request.match_info["rule_id"]
    if not store.delete_rule(rule_id):
        return web.json_response({"error": "Rule not found"}, status=404)
    logger.info("Deleted routing rule %s%s", rule_id, _who(request))
    _broadcast_ws("routing_rules_changed")
    # TG.3: a deleted rule may unblock previously-BLOCKED jobs.
    from routing_eligibility import fire_and_forget  # noqa: PLC0415
    fire_and_forget(request.app)
    return web.json_response({"ok": True})


# Legacy client routes — kept for backwards compatibility

@routes.delete("/ui/api/clients/{client_id}")
async def remove_client(request: web.Request) -> web.Response:
    """Legacy alias for DELETE /ui/api/workers/{client_id}."""
    return await _remove_worker_handler(request, request.match_info["client_id"])


@routes.post("/ui/api/clients/{client_id}/disable")
async def set_client_disabled(request: web.Request) -> web.Response:
    """Legacy alias for POST /ui/api/workers/{client_id}/disable."""
    return await _set_disabled_handler(request, request.match_info["client_id"])


@routes.post("/ui/api/queue/remove")
async def remove_jobs(request: web.Request) -> web.Response:
    """Remove finished jobs from the queue by ID.

    Body: { "ids": ["job-id-1", "job-id-2"] }
    Returns: { "removed": N }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    job_ids = body.get("ids", [])
    if not isinstance(job_ids, list) or not job_ids:
        return web.json_response({"error": "ids must be a non-empty list"}, status=400)

    queue = request.app["queue"]
    removed = await queue.remove_jobs(job_ids)
    return web.json_response({"removed": removed})


@routes.post("/ui/api/queue/clear")
async def clear_queue(request: web.Request) -> web.Response:
    """Remove terminal jobs from the queue permanently.

    Body: { "states": ["success"] }  or  { "states": ["success", "failed", "timed_out"] }
    Returns: { "cleared": N }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    states = body.get("states", [])
    if not isinstance(states, list):
        return web.json_response({"error": "states must be a list"}, status=400)

    require_ota_success = bool(body.get("require_ota_success", False))
    queue = request.app["queue"]
    try:
        cleared = await queue.clear(states, require_ota_success=require_ota_success)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"cleared": cleared})


@routes.get("/ui/api/debug/ha-status")
async def debug_ha_status(request: web.Request) -> web.Response:
    """Debug endpoint: show HA entity status keys and matching info per target."""
    cfg = _cfg(request)
    ha_entity_status: dict[str, dict] = request.app["_rt"].get("ha_entity_status", {})
    ha_mac_set: set[str] = request.app["_rt"].get("ha_mac_set", set())
    device_poller = request.app.get("device_poller")
    targets = scan_configs(cfg.config_dir)

    devices_by_target: dict[str, Device] = {}
    if device_poller:
        for dev in device_poller.get_devices():
            if dev.compile_target:
                devices_by_target[dev.compile_target] = dev

    result: dict = {
        "ha_entity_status_keys": sorted(ha_entity_status.keys()),
        "ha_entity_count": len(ha_entity_status),
        "ha_mac_count": len(ha_mac_set),
        "ha_macs": sorted(ha_mac_set),
        "targets": {},
    }
    for target in targets:
        meta = get_device_metadata(cfg.config_dir, target)
        dev = devices_by_target.get(target)
        device_mac = dev.mac_address if dev else None
        ha_configured, ha_connected, _ha_device_id = _ha_status_for_target(
            ha_entity_status, target, meta, device_mac=device_mac, ha_mac_set=ha_mac_set,
        )
        candidates = []
        friendly = meta.get("friendly_name")
        if friendly:
            candidates.append(_normalize_for_ha(friendly))
        raw_name = meta.get("device_name_raw")
        if raw_name:
            candidates.append(_normalize_for_ha(raw_name))
        candidates.append(_normalize_for_ha(target.replace(".yaml", "")))
        result["targets"][target] = {
            "friendly_name": meta.get("friendly_name"),
            "device_name_raw": meta.get("device_name_raw"),
            "device_mac": device_mac,
            "candidates": candidates,
            "ha_configured": ha_configured,
            "ha_connected": ha_connected,
        }
    return web.json_response(result)


@routes.get("/ui/api/secret-keys")
async def get_secret_keys(request: web.Request) -> web.Response:
    """Return list of secret key names from secrets.yaml (values are never sent)."""
    import yaml  # noqa: PLC0415
    cfg = _cfg(request)
    from constants import SECRETS_YAML  # noqa: PLC0415
    path = Path(cfg.config_dir) / SECRETS_YAML
    if not path.exists():
        return web.json_response({"keys": []})
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return web.json_response({"keys": sorted(str(k) for k in data)})
    except Exception:
        logger.debug("Failed to parse secrets.yaml", exc_info=True)
    return web.json_response({"keys": []})


@routes.post("/ui/api/cancel")
async def cancel_jobs(request: web.Request) -> web.Response:
    """Cancel jobs by id.

    Body: { "job_ids": ["uuid", ...] }
    Returns: { "cancelled": N }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    job_ids = body.get("job_ids", [])
    if not isinstance(job_ids, list):
        return web.json_response({"error": "job_ids must be a list"}, status=400)

    queue = request.app["queue"]
    cancelled = await queue.cancel(job_ids)
    logger.info("Cancelled %d of %d job(s)%s", cancelled, len(job_ids), _who(request))
    return web.json_response({"cancelled": cancelled})


# ---------------------------------------------------------------------------
# Diagnostics (#109) — "Request diagnostics" in the UI.
# ---------------------------------------------------------------------------


@routes.post("/ui/api/diagnostics/server")
async def request_server_diagnostics(request: web.Request) -> web.StreamResponse:
    """Run ``py-spy dump --pid 1`` against the server's own process and
    return the text as an ``attachment`` download. Synchronous because
    the dump itself is fast (<1 s); running it in the request makes the
    UI code trivial (no polling loop for the server side).

    On failure (ptrace denied inside the add-on, py-spy missing, etc.)
    still returns 200 with ``X-Diagnostics-Ok: 0`` so the UI can render
    the returned text as a visible error without treating this as a
    generic 5xx.
    """
    from diagnostics import run_self_thread_dump_async  # noqa: PLC0415

    ok, text = await run_self_thread_dump_async()
    filename = "server-diagnostics.txt"
    logger.info("diagnostics: server self-dump ok=%s len=%d%s", ok, len(text), _who(request))
    return web.Response(
        text=text,
        content_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Diagnostics-Ok": "1" if ok else "0",
        },
    )


@routes.post("/ui/api/workers/{id}/request-diagnostics")
async def request_worker_diagnostics(request: web.Request) -> web.Response:
    """Mint a diagnostics request for the named worker and return its
    ``request_id``. The UI then polls
    ``GET /ui/api/workers/{id}/diagnostics/{request_id}`` until the
    worker has uploaded the dump.
    """
    client_id = request.match_info["id"]
    registry = request.app["registry"]
    worker = registry.get(client_id)
    if worker is None:
        return json_error("Worker not found", 404)
    diag = request.app.get("diagnostics_broker")
    if diag is None:
        return json_error("Diagnostics broker unavailable", 500)
    request_id = diag.request_for_worker(client_id)
    logger.info(
        "diagnostics: requested from worker %s (hostname=%s) request=%s%s",
        client_id, worker.hostname, request_id, _who(request),
    )
    return web.json_response({"request_id": request_id})


@routes.get("/ui/api/workers/{id}/diagnostics/{request_id}")
async def get_worker_diagnostics(request: web.Request) -> web.StreamResponse:
    """Poll endpoint — 202 while the worker hasn't uploaded yet, 200
    with the dump text as an attachment download when it has.

    Mirrors ``/ui/api/diagnostics/server``'s ``X-Diagnostics-Ok`` header
    so the UI can distinguish a real dump from a worker-side error
    (e.g. the worker's own ``py-spy`` attach was denied) without
    parsing text.
    """
    client_id = request.match_info["id"]
    request_id = request.match_info["request_id"]
    diag = request.app.get("diagnostics_broker")
    if diag is None:
        return json_error("Diagnostics broker unavailable", 500)
    result = diag.get_result(request_id)
    if result is None:
        return web.json_response({"pending": True}, status=202)
    registry = request.app["registry"]
    worker = registry.get(client_id)
    host_tag = (worker.hostname if worker else client_id).replace("/", "_")
    filename = f"worker-diagnostics-{host_tag}.txt"
    return web.Response(
        text=result.dump,
        content_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Diagnostics-Ok": "1" if result.ok else "0",
        },
    )
