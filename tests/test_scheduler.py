"""Tests for the APScheduler-based scheduler (#87).

Tests the scheduler module's sync logic and job registration. The actual
fire timing is APScheduler's responsibility — we test that the right jobs
get registered with the right triggers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_queue import JobQueue
from scanner import read_device_meta, write_device_meta


def _write_scheduled_device(
    config_dir: Path,
    name: str = "test-device",
    cron: str = "0 2 * * *",
    enabled: bool = True,
    last_run: str | None = None,
    pin_version: str | None = None,
    schedule_once: str | None = None,
    schedule_tz: str | None = None,
) -> str:
    """Write a minimal YAML with schedule metadata."""
    filename = f"{name}.yaml"
    meta: dict = {}
    if cron:
        meta["schedule"] = cron
        meta["schedule_enabled"] = enabled
    if last_run:
        meta["schedule_last_run"] = last_run
    if pin_version:
        meta["pin_version"] = pin_version
    if schedule_once:
        meta["schedule_once"] = schedule_once
    if schedule_tz:
        meta["schedule_tz"] = schedule_tz

    path = config_dir / filename
    path.write_text(f"esphome:\n  name: {name}\n")
    if meta:
        write_device_meta(str(config_dir), filename, meta)
    return filename


@pytest.fixture
def tmp_queue(tmp_path):
    q = JobQueue(queue_file=tmp_path / "queue.json")
    return q


# ---------------------------------------------------------------------------
# Scheduler sync logic
# ---------------------------------------------------------------------------


async def test_sync_target_adds_cron_job(tmp_path):
    """sync_target registers a cron job for an enabled schedule."""
    import scheduler

    _write_scheduled_device(tmp_path, cron="0 2 * * *", enabled=True)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": JobQueue(queue_file=tmp_path / "q.json"),
    }
    scheduler._app = app
    scheduler._scheduler = None

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler._scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler._scheduler.start()

    try:
        scheduler.sync_target("test-device.yaml")
        jobs = scheduler._scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "sched:test-device.yaml"
        assert jobs[0].next_run_time is not None
    finally:
        scheduler._scheduler.shutdown(wait=False)
        scheduler._scheduler = None
        scheduler._app = None


async def test_sync_target_skips_disabled(tmp_path):
    """sync_target does not register a job for a disabled schedule."""
    import scheduler

    _write_scheduled_device(tmp_path, cron="0 2 * * *", enabled=False)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": JobQueue(queue_file=tmp_path / "q.json"),
    }
    scheduler._app = app

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler._scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler._scheduler.start()

    try:
        scheduler.sync_target("test-device.yaml")
        jobs = scheduler._scheduler.get_jobs()
        assert len(jobs) == 0
    finally:
        scheduler._scheduler.shutdown(wait=False)
        scheduler._scheduler = None
        scheduler._app = None


async def test_sync_target_adds_once_job(tmp_path):
    """sync_target registers a DateTrigger job for schedule_once in the future."""
    import scheduler

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _write_scheduled_device(tmp_path, cron="", enabled=False, schedule_once=future)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": JobQueue(queue_file=tmp_path / "q.json"),
    }
    scheduler._app = app

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler._scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler._scheduler.start()

    try:
        scheduler.sync_target("test-device.yaml")
        jobs = scheduler._scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "once:test-device.yaml"
    finally:
        scheduler._scheduler.shutdown(wait=False)
        scheduler._scheduler = None
        scheduler._app = None


async def test_sync_target_removes_old_job(tmp_path):
    """sync_target replaces the previous job for the same target."""
    import scheduler

    _write_scheduled_device(tmp_path, cron="0 2 * * *", enabled=True)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": JobQueue(queue_file=tmp_path / "q.json"),
    }
    scheduler._app = app

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler._scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler._scheduler.start()

    try:
        scheduler.sync_target("test-device.yaml")
        assert len(scheduler._scheduler.get_jobs()) == 1

        # Now disable the schedule
        meta = read_device_meta(str(tmp_path), "test-device.yaml")
        meta["schedule_enabled"] = False
        write_device_meta(str(tmp_path), "test-device.yaml", meta)

        scheduler.sync_target("test-device.yaml")
        assert len(scheduler._scheduler.get_jobs()) == 0
    finally:
        scheduler._scheduler.shutdown(wait=False)
        scheduler._scheduler = None
        scheduler._app = None


async def test_get_jobs_info(tmp_path):
    """get_jobs_info returns structured data about registered jobs."""
    import scheduler

    _write_scheduled_device(tmp_path, cron="30 14 * * *", enabled=True)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": JobQueue(queue_file=tmp_path / "q.json"),
    }
    scheduler._app = app

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler._scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler._scheduler.start()

    try:
        scheduler.sync_target("test-device.yaml")
        info = scheduler.get_jobs_info()
        assert len(info) == 1
        assert info[0]["id"] == "sched:test-device.yaml"
        assert info[0]["next_run_time"] is not None
    finally:
        scheduler._scheduler.shutdown(wait=False)
        scheduler._scheduler = None
        scheduler._app = None


# ---------------------------------------------------------------------------
# History ring buffer
# ---------------------------------------------------------------------------


async def test_sync_target_uses_schedule_tz(tmp_path):
    """#90: cron is interpreted in `schedule_tz` when present, UTC otherwise."""
    import scheduler

    _write_scheduled_device(
        tmp_path, name="tz-device", cron="0 2 * * *", enabled=True,
        schedule_tz="America/Los_Angeles",
    )
    _write_scheduled_device(
        tmp_path, name="utc-device", cron="0 2 * * *", enabled=True,
    )

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": JobQueue(queue_file=tmp_path / "q.json"),
    }
    scheduler._app = app
    scheduler._scheduler = None

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler._scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler._scheduler.start()

    try:
        scheduler.sync_target("tz-device.yaml")
        scheduler.sync_target("utc-device.yaml")
        tz_job = scheduler._scheduler.get_job("sched:tz-device.yaml")
        utc_job = scheduler._scheduler.get_job("sched:utc-device.yaml")
        # Both fire at 02:00, but in different zones — UTC offsets differ.
        assert str(tz_job.trigger.timezone) == "America/Los_Angeles"
        assert str(utc_job.trigger.timezone) == "UTC"
        # Next-fire times are absolute UTC but originate from different local 02:00s.
        assert tz_job.next_run_time != utc_job.next_run_time
    finally:
        scheduler._scheduler.shutdown(wait=False)
        scheduler._scheduler = None
        scheduler._app = None


async def test_fire_recurring_resolves_ota_address_without_ip(tmp_path):
    """_fire_recurring must resolve OTA address even when dev.ip_address is empty.

    Regression for PR #87 review: the old `and dev.ip_address` guard skipped
    resolve_ota_address when mDNS hadn't yet reported an IP, so schedules
    silently never fired for devices with no cached IP.
    """
    import scheduler
    from unittest.mock import AsyncMock, MagicMock, patch

    _write_scheduled_device(tmp_path, name="no-ip-device")
    target = "no-ip-device.yaml"

    # Device whose ip_address is empty — mDNS hasn't observed it yet.
    fake_dev = MagicMock()
    fake_dev.compile_target = target
    fake_dev.ip_address = ""
    fake_dev.name = "no-ip-device"

    fake_poller = MagicMock()
    fake_poller.get_devices.return_value = [fake_dev]
    fake_poller.resolve_ota_address.return_value = "no-ip-device.local"

    fake_queue = MagicMock()
    fake_queue.enqueue = AsyncMock(return_value=None)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": fake_queue,
        "device_poller": fake_poller,
    }
    scheduler._app = app

    # Inline imports inside _fire_recurring are patched at the source module.
    with patch("scanner.get_esphome_version", return_value="2024.1.0"), \
         patch("git_versioning.get_head", return_value="abc123"), \
         patch("scanner.read_device_meta", return_value={}), \
         patch("scanner.write_device_meta"), \
         patch("scheduler._job_timeout", return_value=600):
        await scheduler._fire_recurring(target)

    fake_poller.resolve_ota_address.assert_called_once_with("no-ip-device")
    enqueue_kwargs = fake_queue.enqueue.call_args.kwargs
    assert enqueue_kwargs["ota_address"] == "no-ip-device.local"


async def test_fire_once_resolves_ota_address_without_ip(tmp_path):
    """_fire_once must also resolve OTA address when dev.ip_address is None."""
    import scheduler
    from unittest.mock import AsyncMock, MagicMock, patch

    _write_scheduled_device(tmp_path, name="no-ip-once")
    target = "no-ip-once.yaml"

    fake_dev = MagicMock()
    fake_dev.compile_target = target
    fake_dev.ip_address = None
    fake_dev.name = "no-ip-once"

    fake_poller = MagicMock()
    fake_poller.get_devices.return_value = [fake_dev]
    fake_poller.resolve_ota_address.return_value = "no-ip-once.local"

    fake_queue = MagicMock()
    fake_queue.enqueue = AsyncMock(return_value=None)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": fake_queue,
        "device_poller": fake_poller,
    }
    scheduler._app = app

    with patch("scanner.get_esphome_version", return_value="2024.1.0"), \
         patch("git_versioning.get_head", return_value="abc123"), \
         patch("scanner.read_device_meta", return_value={}), \
         patch("scanner.write_device_meta"), \
         patch("scheduler._job_timeout", return_value=600):
        await scheduler._fire_once(target)

    fake_poller.resolve_ota_address.assert_called_once_with("no-ip-once")
    enqueue_kwargs = fake_queue.enqueue.call_args.kwargs
    assert enqueue_kwargs["ota_address"] == "no-ip-once.local"


async def test_fire_recurring_auto_detects_thread_target_as_server_ota(tmp_path):
    """SOTA.3: a scheduled recurring fire for a Thread target must enqueue
    with server_ota=True — mirrors the same auto-detection start_compile
    does, so scheduled Thread compiles don't silently try worker-side OTA."""
    import scheduler
    from unittest.mock import AsyncMock, MagicMock, patch

    _write_scheduled_device(tmp_path, name="thread-device")
    target = "thread-device.yaml"

    fake_queue = MagicMock()
    fake_queue.enqueue = AsyncMock(return_value=None)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": fake_queue,
        "device_poller": None,
    }
    scheduler._app = app

    with patch("scanner.get_esphome_version", return_value="2024.1.0"), \
         patch("git_versioning.get_head", return_value="abc123"), \
         patch("scanner.read_device_meta", return_value={}), \
         patch("scanner.write_device_meta"), \
         patch("scanner.get_device_metadata", return_value={"network_type": "thread"}), \
         patch("scheduler._job_timeout", return_value=600):
        await scheduler._fire_recurring(target)

    enqueue_kwargs = fake_queue.enqueue.call_args.kwargs
    assert enqueue_kwargs["server_ota"] is True


async def test_fire_recurring_non_thread_target_not_auto_server_ota(tmp_path):
    import scheduler
    from unittest.mock import AsyncMock, MagicMock, patch

    _write_scheduled_device(tmp_path, name="wifi-device")
    target = "wifi-device.yaml"

    fake_queue = MagicMock()
    fake_queue.enqueue = AsyncMock(return_value=None)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": fake_queue,
        "device_poller": None,
    }
    scheduler._app = app

    with patch("scanner.get_esphome_version", return_value="2024.1.0"), \
         patch("git_versioning.get_head", return_value="abc123"), \
         patch("scanner.read_device_meta", return_value={}), \
         patch("scanner.write_device_meta"), \
         patch("scanner.get_device_metadata", return_value={"network_type": "wifi"}), \
         patch("scheduler._job_timeout", return_value=600):
        await scheduler._fire_recurring(target)

    enqueue_kwargs = fake_queue.enqueue.call_args.kwargs
    assert enqueue_kwargs["server_ota"] is False


async def test_fire_once_auto_detects_thread_target_as_server_ota(tmp_path):
    """SOTA.3: the one-time-schedule fire path gets the same auto-detection
    as _fire_recurring and start_compile."""
    import scheduler
    from unittest.mock import AsyncMock, MagicMock, patch

    _write_scheduled_device(tmp_path, name="thread-once")
    target = "thread-once.yaml"

    fake_queue = MagicMock()
    fake_queue.enqueue = AsyncMock(return_value=None)

    app = {
        "config": type("C", (), {"config_dir": str(tmp_path), "job_timeout": 600})(),
        "queue": fake_queue,
        "device_poller": None,
    }
    scheduler._app = app

    with patch("scanner.get_esphome_version", return_value="2024.1.0"), \
         patch("git_versioning.get_head", return_value="abc123"), \
         patch("scanner.read_device_meta", return_value={}), \
         patch("scanner.write_device_meta"), \
         patch("scanner.get_device_metadata", return_value={"network_type": "thread"}), \
         patch("scheduler._job_timeout", return_value=600):
        await scheduler._fire_once(target)

    enqueue_kwargs = fake_queue.enqueue.call_args.kwargs
    assert enqueue_kwargs["server_ota"] is True


async def test_schedule_history_ring_buffer_caps(tmp_path, tmp_queue):
    """History ring buffer should not exceed _MAX_PER_TARGET entries."""
    import schedule_history

    schedule_history.clear()
    for i in range(60):
        schedule_history.record("test.yaml", datetime.now(timezone.utc), f"job-{i}")

    history = schedule_history.get("test.yaml")
    assert len(history) == schedule_history._MAX_PER_TARGET
