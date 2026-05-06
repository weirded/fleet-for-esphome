"""Tests for the worker REST API (/api/v1/*) in api.py.

Uses aiohttp.test_utils.TestClient/TestServer directly (no pytest-aiohttp required).
"""

from __future__ import annotations

import asyncio
import base64
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
from job_queue import JobQueue, JobState
from main import auth_middleware
from registry import WorkerRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN = "test-secret-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# pytest-homeassistant-custom-component (installed on some dev boxes but not in
# the plain CI test env) pulls in pytest-socket, which globally blocks
# socket() so aiohttp's TestServer can't bind a loopback listener. Tests that
# need loopback access pull this fixture; the pattern mirrors test_ui_api.py.
@pytest.fixture
def _enable_socket():
    try:
        import pytest_socket as _pytest_socket  # type: ignore[import-not-found]
    except ImportError:
        yield
        return
    _pytest_socket.enable_socket()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_bundle() -> bytes:
    """Return a minimal tar.gz that satisfies the bundle contract."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        content = b"esphome:\n  name: testdevice\n"
        info = tarfile.TarInfo(name="testdevice.yaml")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _App:
    """Container for a running TestClient plus direct access to queue and registry."""

    def __init__(
        self,
        client: TestClient,
        queue: JobQueue,
        registry: WorkerRegistry,
        app: web.Application,
    ) -> None:
        self.client = client
        self.queue = queue
        self.registry = registry
        self.app = app

    async def close(self) -> None:
        await self.client.close()

    # Convenience passthroughs so test code stays readable
    async def get(self, *args, **kwargs):
        return await self.client.get(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self.client.post(*args, **kwargs)


async def _make_app(tmp_path: Path, token: str = TOKEN) -> _App:
    """Spin up a fresh isolated test app for a single test."""
    cfg = AppConfig(config_dir=str(tmp_path))
    # SP.8: token comes from Settings now. Wire the scratch store.
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
    # TG.1: every test rig gets a real WorkerTagStore on tmp_path so register
    # tests exercise the full seed/server-wins/overwrite flow rather than the
    # graceful-degrade fallback.
    from worker_tags import WorkerTagStore  # noqa: PLC0415
    app["worker_tag_store"] = WorkerTagStore(path=tmp_path / "worker-tags.json")
    # DQ.2: same shape as worker_tag_store — exercises the full register-seed +
    # heartbeat-push code paths against a real disk store.
    from worker_disk_quotas import WorkerDiskQuotaStore  # noqa: PLC0415
    app["worker_disk_quota_store"] = WorkerDiskQuotaStore(
        path=tmp_path / "worker-disk-quotas.json",
    )
    app.router.add_routes(api_module.routes)

    client = TestClient(TestServer(app))
    await client.start_server()
    return _App(client, queue, registry, app)


async def _enqueue_job(
    queue: JobQueue,
    target: str = "testdevice.yaml",
    version: str = "2024.3.1",
    pinned_client_id: str | None = None,
) -> "JobQueue":  # returns Job
    job = await queue.enqueue(
        target=target,
        esphome_version=version,
        run_id=str(uuid.uuid4()),
        timeout_seconds=300,
        pinned_client_id=pinned_client_id,
    )
    assert job is not None, "enqueue returned None (duplicate?)"
    return job


# Sentinel distinguishing "argument omitted" from "argument explicitly
# set to None". Needed because the helper's two supported shapes — "use
# the live MIN_IMAGE_VERSION" (the default, for tests that don't care)
# and "omit the field entirely" (for tests that want to simulate a
# pre-image_version worker registering) — both need a falsy marker and
# a plain ``None`` default collapsed them into one.
_OMIT_IMAGE_VERSION = object()


async def _register(ta: _App, hostname: str = "build-box", platform: str = "linux/amd64",
                    system_info: dict | None = None,
                    image_version=_OMIT_IMAGE_VERSION) -> str:
    # Bug #21 (1.6.1): default to the live ``MIN_IMAGE_VERSION`` so a
    # future bump doesn't regress every test that uses this helper by
    # silently dropping workers into the stale-image branch. Pre-#21
    # the default was a hardcoded ``"5"`` which broke the moment #6
    # bumped the floor to 7 — ``test_heartbeat_updates_last_seen`` +
    # ``test_heartbeat_returns_server_version`` went red because the
    # heartbeat correctly suppressed ``server_client_version`` for
    # the "fake stale" worker they'd registered without knowing.
    #
    # Three shapes the helper supports:
    #   - default (argument omitted) → send ``MIN_IMAGE_VERSION`` so
    #     the worker passes the stale-image check.
    #   - ``image_version=None`` → omit the field entirely, simulating
    #     a pre-image_version worker (or an actually-stale one with
    #     no field in its register payload).
    #   - explicit string → literal value (e.g. ``"5"`` for "known
    #     stale"). The server enforces the floor with an integer
    #     compare, so string form is fine.
    if image_version is _OMIT_IMAGE_VERSION:
        from constants import MIN_IMAGE_VERSION  # noqa: PLC0415
        image_version = MIN_IMAGE_VERSION
    body: dict = {"hostname": hostname, "platform": platform}
    if system_info is not None:
        body["system_info"] = system_info
    if image_version is not None:
        body["image_version"] = image_version
    resp = await ta.post("/api/v1/workers/register", json=body, headers=AUTH_HEADERS)
    assert resp.status == 200
    return (await resp.json())["client_id"]


# ---------------------------------------------------------------------------
# 1. Registration
# ---------------------------------------------------------------------------

async def test_register_returns_client_id(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "worker1", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert "client_id" in data
        assert len(data["client_id"]) > 0
    finally:
        await ta.close()


async def test_register_stores_worker_in_registry(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "worker1", "platform": "linux/arm64", "client_version": "1.2.3"},
            headers=AUTH_HEADERS,
        )
        client_id = (await resp.json())["client_id"]

        worker = ta.registry.get(client_id)
        assert worker is not None
        assert worker.hostname == "worker1"
        assert worker.platform == "linux/arm64"
        assert worker.client_version == "1.2.3"
    finally:
        await ta.close()


async def test_register_seeds_tags_first_time(tmp_path, _enable_socket):
    """TG.1: WORKER_TAGS-equivalent payload on the first registration seeds the store."""
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/register",
            json={
                "hostname": "tagged-worker",
                "platform": "linux/amd64",
                "tags": ["prod", "linux"],
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        client_id = (await resp.json())["client_id"]
        worker = ta.registry.get(client_id)
        assert worker is not None
        assert worker.tags == ["prod", "linux"]
        # Persisted by identity (hostname).
        assert ta.app["worker_tag_store"].get_tags("tagged-worker") == ["prod", "linux"]
    finally:
        await ta.close()


async def test_register_server_side_wins_after_first(tmp_path, _enable_socket):
    """TG.1: a worker re-registering with different tags doesn't clobber the persisted entry."""
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "dup", "platform": "linux/amd64", "tags": ["prod"]},
            headers=AUTH_HEADERS,
        )
        first_id = (await resp.json())["client_id"]

        # New container, new client_id, but same hostname → server keeps "prod".
        resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "dup", "platform": "linux/amd64", "tags": ["staging", "rebuild"]},
            headers=AUTH_HEADERS,
        )
        second_id = (await resp.json())["client_id"]
        assert second_id != first_id
        worker = ta.registry.get(second_id)
        assert worker.tags == ["prod"]
    finally:
        await ta.close()


async def test_register_overwrite_clobbers(tmp_path, _enable_socket):
    """TG.1: WORKER_TAGS_OVERWRITE=1 → overwrite_tags=true on the wire → server replaces."""
    ta = await _make_app(tmp_path)
    try:
        await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "h1", "platform": "linux/amd64", "tags": ["prod"]},
            headers=AUTH_HEADERS,
        )
        resp = await ta.post(
            "/api/v1/workers/register",
            json={
                "hostname": "h1",
                "platform": "linux/amd64",
                "tags": ["staging", "fast"],
                "overwrite_tags": True,
            },
            headers=AUTH_HEADERS,
        )
        client_id = (await resp.json())["client_id"]
        assert ta.registry.get(client_id).tags == ["staging", "fast"]
        assert ta.app["worker_tag_store"].get_tags("h1") == ["staging", "fast"]
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# DQ.4 — disk-quota seed + push
# ---------------------------------------------------------------------------


async def test_register_seeds_disk_quota_first_time(tmp_path, _enable_socket):
    """DQ.4: WORKER_DISK_QUOTA_GB-equivalent payload on first registration seeds the store."""
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/register",
            json={
                "hostname": "qworker",
                "platform": "linux/amd64",
                "disk_quota_bytes": 5 * 1024 ** 3,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        client_id = (await resp.json())["client_id"]
        worker = ta.registry.get(client_id)
        assert worker is not None
        assert worker.disk_quota_bytes == 5 * 1024 ** 3
        assert ta.app["worker_disk_quota_store"].get_quota("qworker") == 5 * 1024 ** 3
    finally:
        await ta.close()


async def test_register_disk_quota_server_side_wins_after_first(tmp_path, _enable_socket):
    """DQ.4: re-registering with a different env value doesn't clobber the persisted override."""
    ta = await _make_app(tmp_path)
    try:
        await ta.post(
            "/api/v1/workers/register",
            json={
                "hostname": "qworker",
                "platform": "linux/amd64",
                "disk_quota_bytes": 5 * 1024 ** 3,
            },
            headers=AUTH_HEADERS,
        )
        resp = await ta.post(
            "/api/v1/workers/register",
            json={
                "hostname": "qworker",
                "platform": "linux/amd64",
                "disk_quota_bytes": 50 * 1024 ** 3,
            },
            headers=AUTH_HEADERS,
        )
        client_id = (await resp.json())["client_id"]
        # Server kept the original 5 GiB; the worker's restart with a larger
        # env value didn't sneak past the UI's authoritative override.
        assert ta.registry.get(client_id).disk_quota_bytes == 5 * 1024 ** 3
    finally:
        await ta.close()


async def test_heartbeat_pushes_effective_disk_quota(tmp_path, _enable_socket):
    """DQ.4: every heartbeat carries the effective quota (override or fleet default)."""
    import settings as _s
    ta = await _make_app(tmp_path)
    try:
        # Default fleet quota (10 GiB) flows through when no override is set.
        resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "qworker", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        client_id = (await resp.json())["client_id"]

        hb = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        body = await hb.json()
        assert body["set_disk_quota_bytes"] == 10 * 1024 ** 3

        # Set an override → next heartbeat picks it up.
        ta.registry.set_disk_quota(client_id, 3 * 1024 ** 3)
        hb = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        body = await hb.json()
        assert body["set_disk_quota_bytes"] == 3 * 1024 ** 3

        # Bump the fleet default → workers without an override see the new value.
        ta.registry.set_disk_quota(client_id, None)
        await _s.update_settings({"default_worker_disk_quota_bytes": 25 * 1024 ** 3})
        hb = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        body = await hb.json()
        assert body["set_disk_quota_bytes"] == 25 * 1024 ** 3
    finally:
        await ta.close()


async def test_register_without_auth_returns_401(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "worker1", "platform": "linux/amd64"},
        )
        assert resp.status == 401
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 2. Re-registration preserves client_id
# ---------------------------------------------------------------------------

async def test_reregister_preserves_client_id(tmp_path):
    """Re-registering with the same client_id returns the same id."""
    ta = await _make_app(tmp_path)
    try:
        resp1 = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "worker1", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        client_id = (await resp1.json())["client_id"]

        resp2 = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "worker1", "platform": "linux/amd64", "client_id": client_id},
            headers=AUTH_HEADERS,
        )
        assert resp2.status == 200
        assert (await resp2.json())["client_id"] == client_id

        # Only one entry in registry
        assert len(ta.registry.get_all()) == 1
    finally:
        await ta.close()


async def test_reregister_updates_hostname(tmp_path):
    """Re-registration with a new hostname updates the stored value."""
    ta = await _make_app(tmp_path)
    try:
        resp1 = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "old-name", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        client_id = (await resp1.json())["client_id"]

        await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "new-name", "platform": "linux/amd64", "client_id": client_id},
            headers=AUTH_HEADERS,
        )

        worker = ta.registry.get(client_id)
        assert worker.hostname == "new-name"
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 3. Heartbeat
# ---------------------------------------------------------------------------

async def test_heartbeat_updates_last_seen(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        ts_before = ta.registry.get(client_id).last_seen

        await asyncio.sleep(0.01)

        resp = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert "server_client_version" in data

        ts_after = ta.registry.get(client_id).last_seen
        assert ts_after >= ts_before
    finally:
        await ta.close()


async def test_heartbeat_returns_server_version(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        resp = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        data = await resp.json()
        assert isinstance(data["server_client_version"], str)
        assert len(data["server_client_version"]) > 0
    finally:
        await ta.close()


async def test_heartbeat_updates_system_info(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        system_info = {"perf_score": 42, "cpu_usage": 25}
        resp = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id, "system_info": system_info},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        assert ta.registry.get(client_id).system_info == system_info
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 4. Heartbeat — unknown worker
# ---------------------------------------------------------------------------

async def test_heartbeat_unknown_worker_returns_404(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": "does-not-exist"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 404
        data = await resp.json()
        assert "error" in data
    finally:
        await ta.close()


async def test_heartbeat_missing_client_id_returns_400(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/heartbeat",
            json={},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 400
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 5. Claim job — job available
# ---------------------------------------------------------------------------

async def test_claim_job_returns_job_payload(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        await _enqueue_job(ta.queue, "device.yaml")

        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": client_id},
            )

        assert resp.status == 200
        data = await resp.json()
        assert data["target"] == "device.yaml"
        assert "job_id" in data
        assert "bundle_b64" in data
        # Verify the bundle is valid base64
        decoded = base64.b64decode(data["bundle_b64"])
        assert len(decoded) > 0
    finally:
        await ta.close()


async def test_claim_job_transitions_to_working(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")

        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": client_id},
            )

        assert resp.status == 200
        refreshed = ta.queue.get(job.id)
        assert refreshed.state == JobState.WORKING
        assert refreshed.assigned_client_id == client_id
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 6. Claim job — empty queue
# ---------------------------------------------------------------------------

async def test_claim_job_empty_queue_returns_204(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        resp = await ta.get(
            "/api/v1/jobs/next",
            headers={**AUTH_HEADERS, "X-Client-Id": client_id},
        )
        assert resp.status == 204
    finally:
        await ta.close()


async def test_claim_job_missing_client_id_returns_400(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.get("/api/v1/jobs/next", headers=AUTH_HEADERS)
        assert resp.status == 400
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 7. Claim job — disabled worker
# ---------------------------------------------------------------------------

async def test_claim_job_disabled_worker_returns_204(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        await _enqueue_job(ta.queue, "device.yaml")
        ta.registry.set_disabled(client_id, True)

        resp = await ta.get(
            "/api/v1/jobs/next",
            headers={**AUTH_HEADERS, "X-Client-Id": client_id},
        )
        assert resp.status == 204

        # Job should remain PENDING
        assert all(j.state == JobState.PENDING for j in ta.queue.get_all())
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 7b. Claim job — disk-blocked worker (#219)
# ---------------------------------------------------------------------------

async def test_claim_job_disk_blocked_worker_returns_204(tmp_path):
    """A worker stamped with health_blocked_reason="disk_full" must not be
    assigned new jobs even though it's online and not operator-disabled."""
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        await _enqueue_job(ta.queue, "device.yaml")
        # Drive the registry through its real heartbeat path so the
        # hysteresis logic runs (rather than poking the field directly):
        ta.registry.heartbeat(client_id, system_info={"disk_used_pct": 97})
        assert ta.registry.get(client_id).health_blocked_reason == "disk_full"

        resp = await ta.get(
            "/api/v1/jobs/next",
            headers={**AUTH_HEADERS, "X-Client-Id": client_id},
        )
        assert resp.status == 204

        # Job stays PENDING — the disk-blocked worker did not grab it.
        assert all(j.state == JobState.PENDING for j in ta.queue.get_all())

        # Once disk recovers, the same worker resumes claiming on the next poll.
        ta.registry.heartbeat(client_id, system_info={"disk_used_pct": 50})
        assert ta.registry.get(client_id).health_blocked_reason is None
        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp2 = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": client_id},
            )
        assert resp2.status == 200
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 8. Submit result — success and failure
# ---------------------------------------------------------------------------

async def test_submit_result_success(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")
        await ta.queue.claim_next(client_id)

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/result",
            json={"status": "success", "log": "Build complete"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True

        refreshed = ta.queue.get(job.id)
        assert refreshed.state == JobState.SUCCESS
        assert refreshed.log == "Build complete"
    finally:
        await ta.close()


async def test_submit_result_failure(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")
        await ta.queue.claim_next(client_id)

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/result",
            json={"status": "failed", "log": "Compile error"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        assert ta.queue.get(job.id).state == JobState.FAILED
    finally:
        await ta.close()


async def test_submit_result_with_ota_result(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")
        await ta.queue.claim_next(client_id)

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/result",
            json={"status": "success", "log": "done", "ota_result": "success"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        assert ta.queue.get(job.id).ota_result == "success"
    finally:
        await ta.close()


async def test_submit_result_unknown_job_returns_404(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/jobs/nonexistent-id/result",
            json={"status": "success"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 404
    finally:
        await ta.close()


async def test_submit_result_invalid_status_returns_400(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")
        await ta.queue.claim_next(client_id)

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/result",
            json={"status": "broken"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 400
    finally:
        await ta.close()


async def test_submit_result_clears_worker_job(tmp_path):
    """After submitting a result the registry shows current_job_id = None."""
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")
        await ta.queue.claim_next(client_id)
        ta.registry.set_job(client_id, job.id)

        await ta.post(
            f"/api/v1/jobs/{job.id}/result",
            json={"status": "success"},
            headers=AUTH_HEADERS,
        )

        assert ta.registry.get(client_id).current_job_id is None
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 9. Performance-based scheduling — faster worker gets priority
# ---------------------------------------------------------------------------

async def test_faster_worker_gets_job_over_slower(tmp_path):
    """A slower worker defers when a faster idle worker with free slots exists."""
    ta = await _make_app(tmp_path)
    try:
        slow_id = await _register(ta, hostname="slow", system_info={"perf_score": 10, "cpu_usage": 0})
        fast_id = await _register(ta, hostname="fast", system_info={"perf_score": 100, "cpu_usage": 0})

        await _enqueue_job(ta.queue, "device.yaml")

        # Slow worker polls first — should be deferred
        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            slow_resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": slow_id},
            )
        assert slow_resp.status == 204  # deferred — faster worker is idle

        # Fast worker polls — should receive the job
        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            fast_resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": fast_id},
            )
        assert fast_resp.status == 200
        data = await fast_resp.json()
        assert data["target"] == "device.yaml"
    finally:
        await ta.close()


async def test_only_worker_always_gets_job(tmp_path):
    """When there is only one worker, it always claims regardless of perf_score."""
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        # Give the sole worker a poor perf score
        ta.registry.get(client_id).system_info = {"perf_score": 1, "cpu_usage": 99}

        await _enqueue_job(ta.queue, "device.yaml")

        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": client_id},
            )
        assert resp.status == 200
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 10. Pinned job — only designated worker can claim
# ---------------------------------------------------------------------------

async def test_pinned_job_only_claimable_by_pinned_worker(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        id_a = await _register(ta, hostname="worker-a")
        id_b = await _register(ta, hostname="worker-b")

        # Enqueue a job pinned to worker-b
        await _enqueue_job(ta.queue, "device.yaml", pinned_client_id=id_b)

        # Worker-a must not receive it
        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": id_a},
            )
        assert resp.status == 204

        # Worker-b must receive it
        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": id_b},
            )
        assert resp.status == 200
        data = await resp.json()
        assert data["target"] == "device.yaml"
    finally:
        await ta.close()


async def test_pinned_job_not_deferred_by_faster_worker(tmp_path):
    """Pinned jobs ignore the faster-worker deferral logic."""
    ta = await _make_app(tmp_path)
    try:
        slow_id = await _register(ta, hostname="slow",
                                   system_info={"perf_score": 1, "cpu_usage": 0})
        _fast_id = await _register(ta, hostname="fast",
                                    system_info={"perf_score": 100, "cpu_usage": 0})

        # Pin job to the slow worker
        await _enqueue_job(ta.queue, "device.yaml", pinned_client_id=slow_id)

        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": slow_id},
            )

        # Slow worker must NOT be deferred — pinned jobs bypass deferral
        assert resp.status == 200
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# Bug #210 — selection_reason="only_eligible_worker" must NOT fire when
# other workers were eligible-by-tag but happened to be fully booked.
# Pre-fix path attributed busy-blocked competitors as "rules narrowed
# the field" so untagged jobs surfaced "Only eligible by tag" in the UI.
# ---------------------------------------------------------------------------

async def test_busy_other_workers_do_not_trigger_only_eligible_reason(tmp_path, _enable_socket):
    """Two online workers, no routing rules, the second one is fully booked.
    The free worker should claim with reason ``first_available`` — not
    ``only_eligible_worker``, because the busy worker IS eligible by tag.
    """
    ta = await _make_app(tmp_path)
    try:
        free_id = await _register(ta, hostname="free",
                                   system_info={"perf_score": 50, "cpu_usage": 0})
        busy_id = await _register(ta, hostname="busy",
                                   system_info={"perf_score": 50, "cpu_usage": 0})
        # Default max_parallel_jobs == 1 — fill the busy worker's only slot.
        busy_filler = await _enqueue_job(ta.queue, "filler.yaml")
        claimed = await ta.queue.claim_next(busy_id)
        assert claimed is not None and claimed.id == busy_filler.id

        # New untagged job, no routing rules in play.
        await _enqueue_job(ta.queue, "device.yaml")

        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": free_id},
            )
        assert resp.status == 200
        data = await resp.json()
        assert data["target"] == "device.yaml"

        # The freshly claimed job should carry the busy-aware reason —
        # "first_available", not the misleading "only_eligible_worker".
        job = next(j for j in ta.queue.get_all() if j.target == "device.yaml")
        assert job.selection_reason == "first_available"
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 11. Job log streaming (HTTP batch POST)
# ---------------------------------------------------------------------------

async def test_append_log_to_running_job(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")
        await ta.queue.claim_next(client_id)

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/log",
            json={"lines": "Compiling step 1...\nCompiling step 2...\n"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True

        # Log is stored in the streaming buffer (transient)
        refreshed = ta.queue.get(job.id)
        assert "Compiling step 1" in refreshed._streaming_log
    finally:
        await ta.close()


async def test_append_log_unknown_job_returns_404(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/jobs/no-such-job/log",
            json={"lines": "some output"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 404
    finally:
        await ta.close()


async def test_append_log_forwarded_to_subscribers(tmp_path):
    """Lines POSTed to /log are pushed to any registered WebSocket subscribers."""
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")
        await ta.queue.claim_next(client_id)

        received: list[str] = []

        class FakeWs:
            async def send_str(self, text: str) -> None:
                received.append(text)

        fake_ws = FakeWs()
        ta.app["log_subscribers"][job.id] = {fake_ws}

        await ta.post(
            f"/api/v1/jobs/{job.id}/log",
            json={"lines": "hello from worker\n"},
            headers=AUTH_HEADERS,
        )

        assert received == ["hello from worker\n"]
    finally:
        await ta.close()


async def test_append_log_requires_auth(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        job = await _enqueue_job(ta.queue, "device.yaml")
        await ta.queue.claim_next(client_id)

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/log",
            json={"lines": "data"},
        )
        assert resp.status == 401
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# 12. Code endpoint — GET /api/v1/client/code
# ---------------------------------------------------------------------------

async def test_get_client_code_returns_version_and_files(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.get("/api/v1/client/code", headers=AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert "version" in data
        assert "files" in data
        assert isinstance(data["files"], dict)
        # The server module directory contains Python files, so there must be at least one
        assert len(data["files"]) > 0
    finally:
        await ta.close()


async def test_get_client_code_requires_auth(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.get("/api/v1/client/code")
        assert resp.status == 401
    finally:
        await ta.close()


async def test_get_client_version_returns_string(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.get("/api/v1/client/version", headers=AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert "version" in data
        assert isinstance(data["version"], str)
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# Legacy routes — /api/v1/clients/* aliases
# ---------------------------------------------------------------------------

async def test_legacy_register_route_works(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/clients/register",
            json={"hostname": "legacy-worker", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        assert "client_id" in await resp.json()
    finally:
        await ta.close()


async def test_legacy_heartbeat_route_works(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        reg_resp = await ta.post(
            "/api/v1/clients/register",
            json={"hostname": "legacy-worker", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        client_id = (await reg_resp.json())["client_id"]

        resp = await ta.post(
            "/api/v1/clients/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# Deregistration
# ---------------------------------------------------------------------------

async def test_deregister_removes_worker(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        client_id = await _register(ta)
        resp = await ta.post(
            "/api/v1/workers/deregister",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
        assert ta.registry.get(client_id) is None
    finally:
        await ta.close()


async def test_deregister_unknown_worker_returns_404(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/deregister",
            json={"client_id": "ghost-id"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 404
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------

async def test_status_endpoint(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        await _register(ta)
        resp = await ta.get("/api/v1/status", headers=AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert "esphome_version" in data
        assert "online_workers" in data
        assert "queue_size" in data
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# LIB.0 — Docker image version detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("image_version", ["1", "2", "42"])
async def test_register_stores_image_version(tmp_path, image_version):
    """Workers that send image_version have it stored verbatim in the registry."""
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/register",
            json={
                "hostname": "worker",
                "platform": "linux/amd64",
                "client_version": "1.3.0-dev.17",
                "image_version": image_version,
            },
            headers=AUTH_HEADERS,
        )
        client_id = (await resp.json())["client_id"]
        worker = ta.registry.get(client_id)
        assert worker is not None
        assert worker.image_version == image_version
    finally:
        await ta.close()


async def test_register_without_image_version_stores_none(tmp_path):
    """Pre-LIB.0 workers that don't send image_version get None (treated as stale)."""
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "old-worker", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        client_id = (await resp.json())["client_id"]
        worker = ta.registry.get(client_id)
        assert worker is not None
        assert worker.image_version is None
    finally:
        await ta.close()


async def test_heartbeat_advertises_update_for_fresh_image(tmp_path):
    """Workers with a current image_version get server_client_version in heartbeat."""
    from constants import MIN_IMAGE_VERSION
    ta = await _make_app(tmp_path)
    try:
        reg_resp = await ta.post(
            "/api/v1/workers/register",
            json={
                "hostname": "modern-worker",
                "platform": "linux/amd64",
                "image_version": MIN_IMAGE_VERSION,
            },
            headers=AUTH_HEADERS,
        )
        client_id = (await reg_resp.json())["client_id"]

        hb_resp = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        assert hb_resp.status == 200
        data = await hb_resp.json()
        assert "server_client_version" in data
        assert "image_upgrade_required" not in data
    finally:
        await ta.close()


async def test_heartbeat_flags_stale_image(tmp_path):
    """Workers missing image_version get image_upgrade_required, NOT server_client_version."""
    ta = await _make_app(tmp_path)
    try:
        reg_resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "old-worker", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        client_id = (await reg_resp.json())["client_id"]

        hb_resp = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        assert hb_resp.status == 200
        data = await hb_resp.json()
        assert data.get("image_upgrade_required") is True
        assert "min_image_version" in data
        # Suppressing server_client_version prevents the auto-update loop
        assert "server_client_version" not in data
    finally:
        await ta.close()


async def test_heartbeat_flags_below_min_image_version(tmp_path):
    """A reported image_version strictly below the server minimum is flagged."""
    ta = await _make_app(tmp_path)
    try:
        # Pin the server's minimum high enough that "1" is below it for this test
        with patch.object(api_module, "MIN_IMAGE_VERSION", "5"):
            reg_resp = await ta.post(
                "/api/v1/workers/register",
                json={
                    "hostname": "old-image-worker",
                    "platform": "linux/amd64",
                    "image_version": "1",
                },
                headers=AUTH_HEADERS,
            )
            client_id = (await reg_resp.json())["client_id"]

            hb_resp = await ta.post(
                "/api/v1/workers/heartbeat",
                json={"client_id": client_id},
                headers=AUTH_HEADERS,
            )
            data = await hb_resp.json()
            assert data.get("image_upgrade_required") is True
            assert data.get("min_image_version") == "5"
    finally:
        await ta.close()


async def test_get_client_code_refuses_stale_image(tmp_path):
    """Stale-image workers get 409 from /api/v1/client/code instead of code."""
    ta = await _make_app(tmp_path)
    try:
        reg_resp = await ta.post(
            "/api/v1/workers/register",
            json={"hostname": "old-worker", "platform": "linux/amd64"},
            headers=AUTH_HEADERS,
        )
        client_id = (await reg_resp.json())["client_id"]

        headers = {**AUTH_HEADERS, "X-Client-Id": client_id}
        resp = await ta.get("/api/v1/client/code", headers=headers)
        assert resp.status == 409
        data = await resp.json()
        assert data.get("error") == "image_upgrade_required"
    finally:
        await ta.close()


async def test_get_client_code_allows_fresh_image(tmp_path):
    """Fresh-image workers can still pull source code."""
    from constants import MIN_IMAGE_VERSION
    ta = await _make_app(tmp_path)
    try:
        reg_resp = await ta.post(
            "/api/v1/workers/register",
            json={
                "hostname": "modern-worker",
                "platform": "linux/amd64",
                "image_version": MIN_IMAGE_VERSION,
            },
            headers=AUTH_HEADERS,
        )
        client_id = (await reg_resp.json())["client_id"]

        headers = {**AUTH_HEADERS, "X-Client-Id": client_id}
        resp = await ta.get("/api/v1/client/code", headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert "files" in data
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# B.5 — LIB.0 image_version full parametrization
#
# Covers the edge cases not in the existing parametrized test: empty string,
# and garbage non-numeric string. Both must be treated as "stale" by the
# heartbeat response (image_upgrade_required=True), not accepted as valid.
# ---------------------------------------------------------------------------

def _image_version_params() -> list[tuple[str | None, bool]]:
    """Bug #21 (1.6.1): parametrize against the live ``MIN_IMAGE_VERSION``
    constant instead of hardcoding a specific number. The invariant the
    test pins is *'below-min → upgrade_required; at-or-above → fresh'*,
    not a specific version floor — so a bump from 5 → 7 (e.g. the #6
    iputils-ping ship) doesn't regress this suite.
    """
    from constants import MIN_IMAGE_VERSION  # noqa: PLC0415
    min_int = int(MIN_IMAGE_VERSION)
    stale = [(None, True), ("", True), ("garbage", True)]
    stale.extend((str(v), True) for v in range(1, min_int))
    fresh = [
        (str(min_int), False),      # exactly at min
        (str(min_int + 1), False),  # one above
    ]
    return stale + fresh


@pytest.mark.parametrize(
    "image_version,expected_upgrade_required",
    _image_version_params(),
)
async def test_heartbeat_image_version_full_parametrization(
    tmp_path, image_version, expected_upgrade_required,
):
    """Assert the heartbeat branch taken for every image_version edge case.

    When ``image_upgrade_required`` is True the server must also send
    ``min_image_version`` and MUST NOT advertise ``server_client_version``
    (which would cause the worker to attempt an in-place source update that
    can't actually work on a stale Docker image).
    """
    ta = await _make_app(tmp_path)
    try:
        body: dict = {"hostname": "w", "platform": "linux/amd64"}
        if image_version is not None:
            body["image_version"] = image_version

        reg_resp = await ta.post(
            "/api/v1/workers/register",
            json=body,
            headers=AUTH_HEADERS,
        )
        assert reg_resp.status == 200
        client_id = (await reg_resp.json())["client_id"]

        hb_resp = await ta.post(
            "/api/v1/workers/heartbeat",
            json={"client_id": client_id},
            headers=AUTH_HEADERS,
        )
        assert hb_resp.status == 200
        data = await hb_resp.json()

        if expected_upgrade_required:
            assert data.get("image_upgrade_required") is True, (
                f"image_version={image_version!r}: expected image_upgrade_required, got {data}"
            )
            assert "min_image_version" in data
            assert "server_client_version" not in data, (
                "server_client_version must be suppressed for stale images "
                "to prevent the auto-update loop"
            )
        else:
            assert data.get("image_upgrade_required") is None
            assert "server_client_version" in data
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# FD.5 — /api/v1/jobs/{id}/firmware (worker uploads compiled binary)
# ---------------------------------------------------------------------------

async def test_firmware_upload_stores_bin_and_flips_has_firmware(tmp_path, monkeypatch):
    import firmware_storage
    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        assert job is not None
        await _register(ta, hostname="w")
        # Claim moves the job to WORKING.
        await ta.queue.claim_next("any-client")

        # #69: workers now POST to variant-qualified path.
        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware/factory",
            data=b"\xde\xad\xbe\xef" * 100,
            headers={**AUTH_HEADERS, "Content-Type": "application/octet-stream"},
        )
        assert resp.status == 200

        stored = firmware_storage.firmware_path(job.id, variant="factory", root=firmware_dir)
        assert stored.is_file()
        assert stored.read_bytes() == b"\xde\xad\xbe\xef" * 100

        refreshed = ta.queue.get(job.id)
        assert refreshed.has_firmware is True
    finally:
        await ta.close()


async def test_firmware_upload_accepts_non_download_only_job(tmp_path, monkeypatch):
    """Bug #9 (1.6.1): firmware archival covers OTA jobs too.

    Pre-1.6.1 the server refused uploads for ``download_only=False``
    jobs (the previous version of this test asserted the 400). After
    #9 the worker archives every successful compile, so the endpoint
    accepts the upload as long as the job is still WORKING and the
    caller identity matches the assigned worker.
    """
    import firmware_storage
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", tmp_path / "firmware")

    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=False,
        )
        assert job is not None
        await ta.queue.claim_next("any")
        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware",
            data=b"fw",
            headers={**AUTH_HEADERS, "Content-Type": "application/octet-stream"},
        )
        assert resp.status == 200
    finally:
        await ta.close()


async def test_firmware_upload_rejects_missing_job(tmp_path):
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.post(
            "/api/v1/jobs/ghost/firmware",
            data=b"fw",
            headers={**AUTH_HEADERS, "Content-Type": "application/octet-stream"},
        )
        assert resp.status == 404
    finally:
        await ta.close()


async def test_firmware_upload_rejects_empty_body(tmp_path, monkeypatch):
    import firmware_storage
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", tmp_path / "firmware")

    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        assert job is not None
        await ta.queue.claim_next("any")
        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware",
            data=b"",
            headers={**AUTH_HEADERS, "Content-Type": "application/octet-stream"},
        )
        assert resp.status == 400
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# Bug #24 — firmware upload refuses stale workers / wrong state WITHOUT
# writing to disk (preserves a prior successful upload).
# ---------------------------------------------------------------------------

async def test_firmware_upload_rejects_non_working_state_without_writing(
    tmp_path, monkeypatch,
):
    """A job that has already succeeded on another worker must not be
    clobbered by a stale worker's late upload. The server must reject
    with 409 BEFORE touching the on-disk file."""
    import firmware_storage
    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        assert job is not None
        # Successful worker: claim → upload → submit success.
        await ta.queue.claim_next("good-worker")
        firmware_storage.save_firmware(job.id, b"GOOD_FIRMWARE", root=firmware_dir)
        await ta.queue.mark_firmware_stored(job.id)
        await ta.queue.submit_result(job.id, "success", log=None, ota_result=None)

        stored_path = firmware_storage.firmware_path(job.id, root=firmware_dir)
        assert stored_path.read_bytes() == b"GOOD_FIRMWARE"

        # Stale worker arrives late with its own firmware. Must be
        # rejected with 409 AND the good firmware must survive.
        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware",
            data=b"STALE_FIRMWARE_SHOULD_BE_REJECTED",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/octet-stream",
                "X-Client-Id": "stale-worker",
            },
        )
        assert resp.status == 409
        # THE CORE FIX: the file on disk still holds the good worker's
        # firmware. Before bug #24's fix the handler wrote the stale
        # bytes first, then the rejection path deleted the file.
        assert stored_path.read_bytes() == b"GOOD_FIRMWARE"
    finally:
        await ta.close()


async def test_firmware_upload_rejects_unassigned_worker(tmp_path, monkeypatch):
    """Security audit F-08: when the caller's client_id doesn't match
    the currently-assigned worker, reject with 409 even if the job IS
    still WORKING."""
    import firmware_storage
    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        assert job is not None
        await ta.queue.claim_next("assigned-worker")

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware",
            data=b"rogue",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/octet-stream",
                "X-Client-Id": "other-worker",
            },
        )
        assert resp.status == 409
        assert not firmware_storage.firmware_path(job.id, root=firmware_dir).exists()
    finally:
        await ta.close()


async def test_firmware_upload_accepted_for_matching_assigned_worker(
    tmp_path, monkeypatch,
):
    """Happy path: the worker whose client_id matches the assignment
    is accepted and its bytes land on disk."""
    import firmware_storage
    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.3.2", run_id="r",
            timeout_seconds=300, download_only=True,
        )
        assert job is not None
        await ta.queue.claim_next("assigned-worker")

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/firmware/factory",
            data=b"RIGHT_BYTES",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/octet-stream",
                "X-Client-Id": "assigned-worker",
            },
        )
        assert resp.status == 200
        path = firmware_storage.firmware_path(job.id, variant="factory", root=firmware_dir)
        assert path.read_bytes() == b"RIGHT_BYTES"
        assert ta.queue.get(job.id).has_firmware is True
    finally:
        await ta.close()


# ---------------------------------------------------------------------------
# SOTA.1 — server_ota
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_ota_flag_threaded_through_job_assignment(tmp_path):
    """server_ota=True on the Job must appear in the JobAssignment payload."""
    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="thread-dev.yaml", esphome_version="2026.4.3", run_id="r",
            timeout_seconds=300, server_ota=True, ota_address="fd00::1",
        )
        assert job is not None
        assert job.server_ota is True
        client_id = await _register(ta, hostname="w")

        with patch("api.create_bundle_async", new=AsyncMock(return_value=_make_test_bundle())):
            resp = await ta.get(
                "/api/v1/jobs/next",
                headers={**AUTH_HEADERS, "X-Client-Id": client_id},
            )
        assert resp.status == 200
        data = await resp.json()
        assert data["server_ota"] is True
        assert data["ota_address"] == "fd00::1"
    finally:
        await ta.close()


@pytest.mark.asyncio
async def test_patch_ota_result_updates_job(tmp_path):
    """patch_ota_result sets ota_result on an already-SUCCESS job."""
    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.4.3", run_id="r",
            timeout_seconds=300, server_ota=True, ota_address="fd00::1",
        )
        assert job is not None
        await _register(ta, hostname="w")
        await ta.queue.claim_next("any-client")
        await ta.queue.submit_result(job.id, "success", log=None, ota_result=None)

        assert ta.queue.get(job.id).ota_result is None
        await ta.queue.patch_ota_result(job.id, "success", log="OTA done")

        updated = ta.queue.get(job.id)
        assert updated is not None
        assert updated.ota_result == "success"
        assert "OTA done" in (updated.log or "")
    finally:
        await ta.close()


@pytest.mark.asyncio
async def test_patch_ota_result_failed_leaves_job_success(tmp_path):
    """OTA failure from server-side push keeps the job in SUCCESS state."""
    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.4.3", run_id="r",
            timeout_seconds=300, server_ota=True, ota_address="fd00::1",
        )
        assert job is not None
        await _register(ta, hostname="w")
        await ta.queue.claim_next("any-client")
        await ta.queue.submit_result(job.id, "success", log=None, ota_result=None)
        await ta.queue.patch_ota_result(job.id, "failed")

        updated = ta.queue.get(job.id)
        assert updated is not None
        assert updated.state == JobState.SUCCESS
        assert updated.ota_result == "failed"
    finally:
        await ta.close()


@pytest.mark.asyncio
async def test_submit_result_triggers_server_ota_push(tmp_path, monkeypatch):
    """submit_job_result fires _server_ota_push when server_ota=True and binary present."""
    import firmware_storage
    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    push_calls: list = []

    async def _fake_push(app, job):
        push_calls.append(job.id)

    monkeypatch.setattr(api_module, "_server_ota_push", _fake_push)

    ta = await _make_app(tmp_path)
    try:
        job = await ta.queue.enqueue(
            target="x.yaml", esphome_version="2026.4.3", run_id="r",
            timeout_seconds=300, server_ota=True, ota_address="fd00::1",
        )
        assert job is not None
        await _register(ta, hostname="w")
        await ta.queue.claim_next("any-client")

        # Upload firmware (needed for trigger condition)
        firmware_storage.save_firmware(job.id, b"\xff" * 10, variant="ota", root=firmware_dir)
        await ta.queue.mark_firmware_stored(job.id)

        resp = await ta.post(
            f"/api/v1/jobs/{job.id}/result",
            json={"status": "success", "ota_result": None},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200

        # Allow the async task to run
        await asyncio.sleep(0)
        assert push_calls == [job.id]
    finally:
        await ta.close()
