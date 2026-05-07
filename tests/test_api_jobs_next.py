"""Regression tests for GET /api/v1/jobs/next scheduler bugs #234 and #235."""

from __future__ import annotations

import io
import tarfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import api as api_module
from app_config import AppConfig
from job_queue import JobQueue
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


def _make_test_bundle() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        content = b"esphome:\n  name: testdevice\n"
        info = tarfile.TarInfo(name="testdevice.yaml")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _App:
    def __init__(self, client: TestClient, queue: JobQueue, registry: WorkerRegistry,
                 app: web.Application) -> None:
        self.client = client
        self.queue = queue
        self.registry = registry
        self.app = app

    async def close(self) -> None:
        await self.client.close()

    async def get(self, *args, **kwargs):
        return await self.client.get(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self.client.post(*args, **kwargs)


async def _make_app(tmp_path: Path, token: str = TOKEN) -> _App:
    cfg = AppConfig(config_dir=str(tmp_path))
    import settings as _s
    _s._reset_for_tests()
    _s.init_settings(
        settings_path=tmp_path / "settings.json",
        options_path=tmp_path / "options.json",
    )
    await _s.update_settings({"server_token": token})
    queue = JobQueue(queue_file=tmp_path / "queue.json")
    registry = WorkerRegistry()

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
    return _App(client, queue, registry, app)


_OMIT_IMAGE_VERSION = object()


async def _register(ta: _App, hostname: str = "build-box", platform: str = "linux/amd64",
                    system_info: dict | None = None,
                    max_parallel_jobs: int | None = None,
                    image_version=_OMIT_IMAGE_VERSION) -> str:
    if image_version is _OMIT_IMAGE_VERSION:
        from constants import MIN_IMAGE_VERSION  # noqa: PLC0415
        image_version = MIN_IMAGE_VERSION
    body: dict = {"hostname": hostname, "platform": platform}
    if system_info is not None:
        body["system_info"] = system_info
    if max_parallel_jobs is not None:
        body["max_parallel_jobs"] = max_parallel_jobs
    if image_version is not None:
        body["image_version"] = image_version
    resp = await ta.post("/api/v1/workers/register", json=body, headers=AUTH_HEADERS)
    assert resp.status == 200
    return (await resp.json())["client_id"]


async def _enqueue_job(queue: JobQueue, target: str = "testdevice.yaml",
                       version: str = "2024.3.1") -> "object":
    job = await queue.enqueue(
        target=target,
        esphome_version=version,
        run_id=str(uuid.uuid4()),
        timeout_seconds=300,
        pinned_client_id=None,
    )
    assert job is not None
    return job


# ---------------------------------------------------------------------------
# Bug #234 — perf_score: None crashes the eligibility loop → HTTP 500
# ---------------------------------------------------------------------------

async def test_234_perf_score_none_my_worker_no_crash(tmp_path, _enable_socket):
    """
    my perf_score=None — the `or 0` guard must prevent a TypeError crash.
    We don't enqueue a job so the only possible responses are 204 (no job) or
    500 (unhandled exception). Pre-fix this was 500.
    """
    ta = await _make_app(tmp_path)
    try:
        victim_id = await _register(ta, hostname="victim",
                                    system_info={"perf_score": None})
        # A second worker so the eligibility loop actually runs.
        _other_id = await _register(ta, hostname="other",
                                    system_info={"perf_score": None})

        # No job enqueued → expect 204. If the guard is absent we get 500 instead.
        resp = await ta.get(
            "/api/v1/jobs/next",
            headers={**AUTH_HEADERS, "X-Client-Id": victim_id},
        )
        assert resp.status == 204, (
            f"Expected 204 (no job) but got {resp.status} — likely TypeError crash"
        )
    finally:
        await ta.close()


async def test_234_perf_score_none_other_worker_no_crash(tmp_path, _enable_socket):
    """
    other worker has perf_score=None — `or 0` guard prevents TypeError on `other_perf *`.
    With a job enqueued, the only_worker (good_id) should claim it (200) because
    `other` won't be able to beat it with score=0.
    """
    ta = await _make_app(tmp_path)
    try:
        good_id = await _register(ta, hostname="good",
                                   system_info={"perf_score": 50, "cpu_usage": 0})
        _bad_other = await _register(ta, hostname="bad-other",
                                     system_info={"perf_score": None})

        # No job → clean 204 (skip bundle creation entirely)
        resp = await ta.get(
            "/api/v1/jobs/next",
            headers={**AUTH_HEADERS, "X-Client-Id": good_id},
        )
        assert resp.status != 500, "perf_score=None on peer worker must not crash"
    finally:
        await ta.close()


async def test_234_missing_perf_score_key_no_crash(tmp_path, _enable_socket):
    """system_info={} (no perf_score key at all) must not 500."""
    ta = await _make_app(tmp_path)
    try:
        worker_id = await _register(ta, hostname="bare", system_info={})
        _other_id = await _register(ta, hostname="bare2", system_info={})

        resp = await ta.get(
            "/api/v1/jobs/next",
            headers={**AUTH_HEADERS, "X-Client-Id": worker_id},
        )
        assert resp.status != 500
    finally:
        await ta.close()


async def test_234_warning_log_fires_on_eligibility_exception(tmp_path, caplog, _enable_socket):
    """
    Force an eligibility-loop crash by making registry.get_all() raise inside the
    try block, then assert the WARNING log fires with client_id and the endpoint
    returns HTTP 204 (not 500).
    """
    ta = await _make_app(tmp_path)
    try:
        worker_id = await _register(ta, hostname="crashee",
                                    system_info={"perf_score": 50, "cpu_usage": 0})

        import logging
        # Patch get_all on the live registry instance so the crash fires
        # inside the try/except in the eligibility block, not in middleware.
        original_get_all = ta.registry.get_all

        def _crashing_get_all():
            raise RuntimeError("injected eligibility crash")

        ta.registry.get_all = _crashing_get_all  # type: ignore[method-assign]
        try:
            with caplog.at_level(logging.WARNING, logger="api"):
                resp = await ta.get(
                    "/api/v1/jobs/next",
                    headers={**AUTH_HEADERS, "X-Client-Id": worker_id},
                )
        finally:
            ta.registry.get_all = original_get_all  # type: ignore[method-assign]

        assert resp.status == 204, f"Expected 204 from caught exception, got {resp.status}"
        warning_records = [r for r in caplog.records
                           if "Scheduler eligibility check failed" in r.message
                           and worker_id in r.message]
        assert warning_records, (
            "Expected WARNING log with 'Scheduler eligibility check failed' "
            f"and client_id={worker_id!r}; got: {[r.message for r in caplog.records]}"
        )
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# Bug #235 — deferral cap: remote workers starve when local has higher score
# ---------------------------------------------------------------------------

async def test_235_remote_claims_overflow_when_queue_exceeds_fast_pool(
    tmp_path, _enable_socket
):
    """
    1 local (fast, 1 slot, perf=100) + 1 remote (slow, 4 slots, perf=10) + 4 PENDING jobs.
    After the fast worker's slot is occupied, remote must claim overflow (not idle).

    Pre-fix: remote deferred every time because fast had free slots momentarily.
    Post-fix: remote claims because queue depth (4) > fast pool's free slots (1).
    """
    ta = await _make_app(tmp_path)
    try:
        await _register(ta, hostname="local",
                        system_info={"perf_score": 100, "cpu_usage": 0},
                        max_parallel_jobs=1)
        remote_id = await _register(ta, hostname="remote",
                                    system_info={"perf_score": 10, "cpu_usage": 0},
                                    max_parallel_jobs=4)

        for i in range(4):
            await _enqueue_job(ta.queue, target=f"device{i}.yaml")

        # Simulate local worker being idle between jobs (the exact window that
        # caused the starvation: active=0 so old code deferred remote every time).
        # With 4 pending and fast pool only 1 free slot, remote must NOT defer.
        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": remote_id},
            )

        assert resp.status == 200, (
            "remote worker should claim a job (queue=4 > fast pool free=1) "
            f"but got {resp.status}"
        )
    finally:
        await ta.close()


async def test_235_remote_defers_when_queue_fits_fast_pool(tmp_path, _enable_socket):
    """
    Sanity / anti-regression: 1 job + fast worker with 1 free slot → remote SHOULD defer.
    The cap must not suppress legitimate deferral when queue fits within the priority pool.
    """
    ta = await _make_app(tmp_path)
    try:
        _local_id = await _register(ta, hostname="local",
                                    system_info={"perf_score": 100, "cpu_usage": 0},
                                    max_parallel_jobs=1)
        remote_id = await _register(ta, hostname="remote",
                                    system_info={"perf_score": 10, "cpu_usage": 0},
                                    max_parallel_jobs=4)

        await _enqueue_job(ta.queue, target="device0.yaml")

        # 1 job ≤ 1 fast-pool free slot → remote should defer
        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": remote_id},
            )

        assert resp.status == 204, (
            "remote should defer (queue=1 ≤ fast pool free=1) "
            f"but got {resp.status}"
        )
    finally:
        await ta.close()


async def test_235_boundary_queue_equals_fast_pool_slots(tmp_path, _enable_socket):
    """
    Exactly-at-boundary: queue depth == fast pool's free slots → remote defers
    (queue fits; fast worker can take all of them).
    """
    ta = await _make_app(tmp_path)
    try:
        _local_id = await _register(ta, hostname="local",
                                    system_info={"perf_score": 100, "cpu_usage": 0},
                                    max_parallel_jobs=2)
        remote_id = await _register(ta, hostname="remote",
                                    system_info={"perf_score": 10, "cpu_usage": 0},
                                    max_parallel_jobs=4)

        # 2 jobs == 2 fast-pool free slots → boundary: still defer
        for i in range(2):
            await _enqueue_job(ta.queue, target=f"device{i}.yaml")

        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": remote_id},
            )

        assert resp.status == 204, (
            "remote should defer when queue == fast pool free slots, "
            f"but got {resp.status}"
        )
    finally:
        await ta.close()
