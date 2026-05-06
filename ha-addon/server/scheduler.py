"""APScheduler-based cron scheduler for per-device compile+OTA (#87).

Replaces the DIY schedule_checker loop with APScheduler's AsyncIOScheduler,
which handles cron parsing, next-fire computation, misfire grace, and
persistence correctly out of the box. Schedule state is still stored in the
YAML comment block (source of truth) — APScheduler is the execution engine.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]

import schedule_history

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_app: Any = None  # aiohttp app reference for access to queue/config/device_poller


def _job_id(target: str) -> str:
    return f"sched:{target}"


def _job_timeout() -> int:
    """SP.8: read the live job_timeout from Settings so drawer edits
    propagate to scheduled fires without a restart."""
    from settings import get_settings  # noqa: PLC0415
    return get_settings().job_timeout


async def _fire_recurring(target: str) -> None:
    """Callback: enqueue a compile+OTA job for a recurring schedule."""
    from scanner import read_device_meta, write_device_meta, get_esphome_version  # noqa: PLC0415

    if _app is None:
        return
    queue = _app["queue"]
    cfg = _app["config"]

    meta = read_device_meta(cfg.config_dir, target)
    version = meta.get("pin_version") or get_esphome_version()

    device_poller = _app.get("device_poller")
    ota_address = None
    if device_poller:
        for dev in device_poller.get_devices():
            if dev.compile_target == target:
                # Bug #18 (1.6.1): shared best-address helper — picks
                # a real IP over a stale ``.local`` fallback.
                # Don't gate on dev.ip_address: resolve_ota_address
                # provides a .local fallback even when mDNS hasn't
                # reported an IP yet.
                ota_address = device_poller.resolve_ota_address(dev.name)
                break

    run_id = str(uuid.uuid4())
    from git_versioning import get_head  # noqa: PLC0415
    from pathlib import Path as _P  # noqa: PLC0415
    from scanner import get_device_metadata as _gdm  # noqa: PLC0415
    _tmeta = _gdm(cfg.config_dir, target)
    server_ota = _tmeta.get("network_type") == "thread"
    job = await queue.enqueue(
        target=target,
        esphome_version=version,
        run_id=run_id,
        timeout_seconds=_job_timeout(),
        server_ota=server_ota,
        ota_address=ota_address,
        config_hash=get_head(_P(cfg.config_dir)),
    )
    if job is not None:
        job.scheduled = True
        job.schedule_kind = "recurring"
        schedule_history.record(target, datetime.now(timezone.utc), job.id)
        logger.info("Schedule fired for %s: enqueued job %s (version=%s)", target, job.id, version)
        # TG.3: route the freshly-enqueued job through the rule engine.
        from routing_eligibility import fire_and_forget  # noqa: PLC0415
        fire_and_forget(_app)

    fresh_meta = read_device_meta(cfg.config_dir, target)
    fresh_meta["schedule_last_run"] = datetime.now(timezone.utc).isoformat()
    write_device_meta(cfg.config_dir, target, fresh_meta)


async def _fire_once(target: str) -> None:
    """Callback: enqueue a one-time compile+OTA, then clear the schedule_once."""
    from scanner import read_device_meta, write_device_meta, get_esphome_version  # noqa: PLC0415

    if _app is None:
        return
    queue = _app["queue"]
    cfg = _app["config"]

    meta = read_device_meta(cfg.config_dir, target)
    version = meta.get("pin_version") or get_esphome_version()

    device_poller = _app.get("device_poller")
    ota_address = None
    if device_poller:
        for dev in device_poller.get_devices():
            if dev.compile_target == target:
                # Bug #18 (1.6.1): shared best-address helper — picks
                # a real IP over a stale ``.local`` fallback.
                # Don't gate on dev.ip_address: resolve_ota_address
                # provides a .local fallback even when mDNS hasn't
                # reported an IP yet.
                ota_address = device_poller.resolve_ota_address(dev.name)
                break

    run_id = str(uuid.uuid4())
    from git_versioning import get_head  # noqa: PLC0415
    from pathlib import Path as _P  # noqa: PLC0415
    from scanner import get_device_metadata as _gdm  # noqa: PLC0415
    _tmeta = _gdm(cfg.config_dir, target)
    server_ota = _tmeta.get("network_type") == "thread"
    job = await queue.enqueue(
        target=target,
        esphome_version=version,
        run_id=run_id,
        timeout_seconds=_job_timeout(),
        server_ota=server_ota,
        ota_address=ota_address,
        config_hash=get_head(_P(cfg.config_dir)),
    )
    if job is not None:
        job.scheduled = True
        job.schedule_kind = "once"
        schedule_history.record(target, datetime.now(timezone.utc), job.id)
        logger.info("One-time schedule fired for %s: enqueued job %s", target, job.id)
        # TG.3: route the freshly-enqueued job through the rule engine.
        from routing_eligibility import fire_and_forget  # noqa: PLC0415
        fire_and_forget(_app)

    fresh_meta = read_device_meta(cfg.config_dir, target)
    fresh_meta.pop("schedule_once", None)
    write_device_meta(cfg.config_dir, target, fresh_meta)


def start(app: object) -> None:
    """Start the APScheduler and load all schedules from YAML metadata."""
    global _scheduler, _app
    _app = app

    _scheduler = AsyncIOScheduler(
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        },
        timezone="UTC",
    )
    _scheduler.start()
    logger.info("APScheduler started")

    sync_all_from_yaml()


def stop() -> None:
    """Shut down the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
        _scheduler = None


def sync_all_from_yaml() -> None:
    """Rebuild all APScheduler jobs from the YAML comment blocks.

    Called on startup and can be called after config scans to pick up
    manual YAML edits.
    """
    if _scheduler is None or _app is None:
        return

    from scanner import scan_configs, read_device_meta  # noqa: PLC0415

    cfg = _app["config"]
    targets = scan_configs(cfg.config_dir)

    # Remove all existing schedule jobs
    existing_jobs = {j.id for j in _scheduler.get_jobs()}
    for jid in existing_jobs:
        if jid.startswith("sched:") or jid.startswith("once:"):
            _scheduler.remove_job(jid)

    added = 0
    for target in targets:
        meta = read_device_meta(cfg.config_dir, target)
        added += _sync_target(target, meta)

    logger.info("Synced %d schedule(s) from %d targets", added, len(targets))


def sync_target(target: str) -> None:
    """Re-sync a single target's schedule after an API mutation."""
    if _scheduler is None or _app is None:
        return

    from scanner import read_device_meta  # noqa: PLC0415
    cfg = _app["config"]
    meta = read_device_meta(cfg.config_dir, target)

    # Remove existing jobs for this target
    for jid in [_job_id(target), f"once:{target}"]:
        try:
            _scheduler.remove_job(jid)
        except Exception:
            pass

    _sync_target(target, meta)


def _sync_target(target: str, meta: dict) -> int:
    """Add APScheduler jobs for a target based on its metadata. Returns count added."""
    if _scheduler is None:
        return 0

    added = 0

    # Recurring cron schedule
    cron_expr = meta.get("schedule")
    enabled = meta.get("schedule_enabled", False)
    # #90: tz-aware schedules. The cron expression is interpreted in
    # `schedule_tz` (an IANA tz name like "America/Los_Angeles"). When absent,
    # default to UTC for backward compat with schedules created before #90.
    cron_tz = meta.get("schedule_tz") or "UTC"
    if cron_expr and enabled:
        parts = cron_expr.strip().split()
        if len(parts) == 5:
            try:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                    timezone=cron_tz,
                )
                _scheduler.add_job(
                    _fire_recurring,
                    trigger=trigger,
                    args=[target],
                    id=_job_id(target),
                    name=f"Schedule: {target}",
                    replace_existing=True,
                )
                added += 1
                next_fire = _scheduler.get_job(_job_id(target)).next_run_time
                logger.debug("Added cron job for %s (%s), next fire: %s", target, cron_expr, next_fire)
            except Exception:
                logger.warning("Invalid cron expression for %s: %s", target, cron_expr, exc_info=True)

    # One-time schedule
    once_str = meta.get("schedule_once")
    if once_str:
        try:
            once_dt = datetime.fromisoformat(once_str)
            if once_dt.tzinfo is None:
                once_dt = once_dt.replace(tzinfo=timezone.utc)
            if once_dt > datetime.now(timezone.utc):
                trigger = DateTrigger(run_date=once_dt, timezone="UTC")
                _scheduler.add_job(
                    _fire_once,
                    trigger=trigger,
                    args=[target],
                    id=f"once:{target}",
                    name=f"Once: {target}",
                    replace_existing=True,
                )
                added += 1
                logger.debug("Added one-time job for %s at %s", target, once_dt)
            else:
                # Past due — fire immediately if within grace
                grace = 300
                if (datetime.now(timezone.utc) - once_dt).total_seconds() <= grace:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    loop.create_task(_fire_once(target))
                    added += 1
                else:
                    logger.warning("One-time schedule for %s missed by %ds — clearing",
                                   target, int((datetime.now(timezone.utc) - once_dt).total_seconds()))
                    from scanner import read_device_meta as _rdm, write_device_meta  # noqa: PLC0415
                    cfg = _app["config"]
                    fresh = _rdm(cfg.config_dir, target)
                    fresh.pop("schedule_once", None)
                    write_device_meta(cfg.config_dir, target, fresh)
        except Exception:
            logger.warning("Invalid one-time schedule for %s: %s", target, once_str, exc_info=True)

    return added


def get_jobs_info() -> list[dict]:
    """Return info about all scheduled jobs (for the debug endpoint)."""
    if _scheduler is None:
        return []
    result = []
    for job in _scheduler.get_jobs():
        result.append({
            "id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return result
