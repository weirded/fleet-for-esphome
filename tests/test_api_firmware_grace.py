"""Regression tests for bug #236: server-side grace window for late
firmware-variant uploads from the still-assigned worker.

The reporter (Unraid + docker-compose worker, ~minute-long upload of
both factory + ota variants) saw HTTP 409 ``job_not_working`` on the
SECOND variant upload of a successful flash because the server's
timeout-checker had flipped the job mid-upload. The worker-side
ordering is already correct on develop (every success path uploads
variants before submit_result), so the fix is server-side: tolerate
late uploads from the still-assigned worker for a short grace window
after the terminal-state transition.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import api as api_module
from app_config import AppConfig
from job_queue import JobQueue, JobState, _utcnow
from main import auth_middleware
from registry import WorkerRegistry

TOKEN = "test-secret-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def _enable_socket():
    try:
        import pytest_socket as _ps  # type: ignore[import-not-found]
    except ImportError:
        yield
        return
    _ps.enable_socket()
    yield


class _App:
    def __init__(self, client: TestClient, queue: JobQueue, registry: WorkerRegistry,
                 firmware_dir: Path, app: web.Application) -> None:
        self.client = client
        self.queue = queue
        self.registry = registry
        self.firmware_dir = firmware_dir
        self.app = app

    async def close(self) -> None:
        await self.client.close()

    async def post(self, *args, **kwargs):
        return await self.client.post(*args, **kwargs)


async def _make_app(tmp_path: Path) -> _App:
    cfg = AppConfig(config_dir=str(tmp_path))
    import settings as _s
    _s._reset_for_tests()
    _s.init_settings(
        settings_path=tmp_path / "settings.json",
        options_path=tmp_path / "options.json",
    )
    await _s.update_settings({"server_token": TOKEN})

    queue = JobQueue(queue_file=tmp_path / "queue.json")
    registry = WorkerRegistry()
    firmware_dir = tmp_path / "firmware"
    firmware_dir.mkdir()
    # The API handler doesn't take a `root=` override; firmware_storage
    # reads ``DEFAULT_FIRMWARE_DIR`` at call time via _resolve_root(), so
    # monkeypatching the module global is sufficient.
    import firmware_storage as _fs
    _fs.DEFAULT_FIRMWARE_DIR = firmware_dir

    app = web.Application(middlewares=[auth_middleware])
    app["config"] = cfg
    app["queue"] = queue
    app["registry"] = registry
    app["log_subscribers"] = {}
    from worker_tags import WorkerTagStore  # noqa: PLC0415
    app["worker_tag_store"] = WorkerTagStore(path=tmp_path / "worker-tags.json")
    from worker_disk_quotas import WorkerDiskQuotaStore  # noqa: PLC0415
    app["worker_disk_quota_store"] = WorkerDiskQuotaStore(
        path=tmp_path / "worker-disk-quotas.json",
    )
    app.router.add_routes(api_module.routes)

    client = TestClient(TestServer(app))
    await client.start_server()
    return _App(client, queue, registry, firmware_dir, app)


async def _enqueue_and_assign(queue: JobQueue, client_id: str) -> "object":
    job = await queue.enqueue(
        target="testdevice.yaml",
        esphome_version="2024.3.1",
        run_id=str(uuid.uuid4()),
        timeout_seconds=300,
        pinned_client_id=None,
    )
    assert job is not None
    # Mark as WORKING + assigned to client_id, then transition to SUCCESS
    # so finished_at is set. Mirrors the real lifecycle.
    job.state = JobState.WORKING
    job.assigned_client_id = client_id
    return job


# ---------------------------------------------------------------------------
# Bug #236 — server-side grace window for late firmware uploads
# ---------------------------------------------------------------------------

async def test_236_late_upload_within_grace_window_accepted(tmp_path, _enable_socket):
    """Job just transitioned to SUCCESS; assigned worker uploads variant — accept."""
    ta = await _make_app(tmp_path)
    try:
        client_id = "worker-A"
        job = await _enqueue_and_assign(ta.queue, client_id)
        # Simulate the worker calling submit_result(success) just before
        # finishing the variant upload.
        job.state = JobState.SUCCESS
        job.finished_at = _utcnow()  # 0 seconds ago — well within grace

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware/factory",
            data=b"FAKE_FACTORY_BIN",
            headers={**AUTH_HEADERS, "X-Client-Id": client_id},
        )
        assert resp.status == 200, await resp.text()
        # has_firmware should now be True so the UI surfaces the variant.
        assert ta.queue.get(job.id).has_firmware is True
        # Bytes landed on disk.
        from firmware_storage import firmware_path  # noqa: PLC0415
        path = firmware_path(job.id, "factory", root=ta.firmware_dir)
        assert path.exists()
        assert path.read_bytes() == b"FAKE_FACTORY_BIN"
    finally:
        await ta.close()


async def test_236_late_upload_outside_grace_window_rejected(tmp_path, _enable_socket):
    """Job finished too long ago — the worker is genuinely stale. Reject 409."""
    ta = await _make_app(tmp_path)
    try:
        client_id = "worker-A"
        job = await _enqueue_and_assign(ta.queue, client_id)
        job.state = JobState.SUCCESS
        job.finished_at = _utcnow() - timedelta(seconds=120)  # > 60s grace

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware/factory",
            data=b"FAKE_FACTORY_BIN",
            headers={**AUTH_HEADERS, "X-Client-Id": client_id},
        )
        assert resp.status == 409
        body = await resp.json()
        assert body.get("error") == "job_not_working"
    finally:
        await ta.close()


async def test_236_late_upload_from_wrong_worker_rejected(tmp_path, _enable_socket):
    """Different worker tries to upload after job finished — reject even within grace."""
    ta = await _make_app(tmp_path)
    try:
        assigned_id = "worker-A"
        job = await _enqueue_and_assign(ta.queue, assigned_id)
        job.state = JobState.SUCCESS
        job.finished_at = _utcnow()  # within grace

        # A DIFFERENT worker tries to upload.
        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware/factory",
            data=b"BOGUS_BIN",
            headers={**AUTH_HEADERS, "X-Client-Id": "worker-B"},
        )
        assert resp.status == 409
        body = await resp.json()
        assert body.get("error") == "job_not_working"
    finally:
        await ta.close()


async def test_236_working_path_still_works(tmp_path, _enable_socket):
    """Sanity: pre-#236 happy path (job WORKING) still returns 200 + flips flag."""
    ta = await _make_app(tmp_path)
    try:
        client_id = "worker-A"
        job = await _enqueue_and_assign(ta.queue, client_id)
        # Job stays WORKING — no finished_at.

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware/ota",
            data=b"OTA_BIN",
            headers={**AUTH_HEADERS, "X-Client-Id": client_id},
        )
        assert resp.status == 200, await resp.text()
        assert ta.queue.get(job.id).has_firmware is True
    finally:
        await ta.close()
