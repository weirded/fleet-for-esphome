"""Tests for the queue-depth metrics endpoint (Bug #168, 1.7.2).

Pins the shape of ``GET /api/v1/metrics/queue`` so an external autoscaler
(KEDA's ``metrics-api`` scaler, HPA via external metrics, Sablier, the
in-tree Proxmox scaler) can rely on the JSON field set, auth gate, and
counting behaviour.

The endpoint sits under ``/api/v1/*`` so the existing Bearer-token
middleware applies; trust boundary matches worker-claim endpoints
(read-only metric, same token).
"""

from __future__ import annotations

import uuid

from tests.test_api import (  # type: ignore[import-not-found]
    AUTH_HEADERS,
    _enqueue_job,
    _make_app,
    _register,
)


async def test_metrics_queue_returns_zero_baseline(tmp_path):
    """Empty queue + no workers → all counts zero, schema_version set."""
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.get("/api/v1/metrics/queue", headers=AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data == {
            "pending": 0,
            "working": 0,
            "active": 0,
            "online_workers": 0,
            "max_parallel_capacity": 0,
            "schema_version": 1,
        }
    finally:
        await ta.close()


async def test_metrics_queue_counts_pending_and_working(tmp_path):
    """Two PENDING jobs + one WORKING job → pending=2, working=1, active=3."""
    ta = await _make_app(tmp_path)
    try:
        from job_queue import JobState  # noqa: PLC0415

        await _enqueue_job(ta.queue, "a.yaml")
        await _enqueue_job(ta.queue, "b.yaml")
        third = await _enqueue_job(ta.queue, "c.yaml")
        third.state = JobState.WORKING
        third.assigned_client_id = "worker-x"

        resp = await ta.get("/api/v1/metrics/queue", headers=AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["pending"] == 2
        assert data["working"] == 1
        assert data["active"] == 3
        assert data["schema_version"] == 1
    finally:
        await ta.close()


async def test_metrics_queue_reports_online_capacity(tmp_path):
    """Two registered workers (parallel = 2 + 4) → online_workers=2,
    max_parallel_capacity=6."""
    ta = await _make_app(tmp_path)
    try:
        # First worker — 2 slots.
        await _register(ta, hostname="alpha")
        alpha = ta.registry.get_all()[0]
        alpha.max_parallel_jobs = 2
        # Second worker — 4 slots.
        await _register(ta, hostname="beta")
        beta = next(w for w in ta.registry.get_all() if w.hostname == "beta")
        beta.max_parallel_jobs = 4

        resp = await ta.get("/api/v1/metrics/queue", headers=AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["online_workers"] == 2
        assert data["max_parallel_capacity"] == 6
    finally:
        await ta.close()


async def test_metrics_queue_requires_bearer_token(tmp_path):
    """No auth → 401 (worker-tier endpoint, never publicly readable)."""
    ta = await _make_app(tmp_path)
    try:
        resp = await ta.get("/api/v1/metrics/queue")
        assert resp.status == 401
    finally:
        await ta.close()


async def test_metrics_queue_ignores_terminal_jobs(tmp_path):
    """SUCCESS/FAILED/CANCELLED don't count toward pending or working."""
    ta = await _make_app(tmp_path)
    try:
        from job_queue import JobState  # noqa: PLC0415

        finished = await _enqueue_job(ta.queue, "done.yaml")
        finished.state = JobState.SUCCESS
        failed = await _enqueue_job(ta.queue, "broken.yaml")
        failed.state = JobState.FAILED
        await _enqueue_job(ta.queue, "live.yaml")  # stays PENDING

        resp = await ta.get("/api/v1/metrics/queue", headers=AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["pending"] == 1
        assert data["working"] == 0
        assert data["active"] == 1
    finally:
        await ta.close()
