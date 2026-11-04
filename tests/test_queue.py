"""Unit tests for JobQueue — state machine, persistence, timeouts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_queue import JobQueue, JobState, MAX_LOG_BYTES, LOG_TRUNCATED_MARKER, MAX_RETRIES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc)


async def _enqueue(q: JobQueue, target: str = "device.yaml", *, version: str = "2024.3.1", run_id: str = "run1", timeout: int = 300, **kwargs):
    return await q.enqueue(target, version, run_id, timeout, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_queue_file(tmp_path):
    return tmp_path / "queue.json"


@pytest.fixture
def queue(tmp_queue_file):
    return JobQueue(queue_file=tmp_queue_file)


# ---------------------------------------------------------------------------
# Enqueue / dequeue (FIFO + deduplication)
# ---------------------------------------------------------------------------

async def test_enqueue_returns_job(queue):
    job = await _enqueue(queue, "device1.yaml")
    assert job is not None
    assert job.target == "device1.yaml"
    assert job.state == JobState.PENDING


async def test_enqueue_multiple_targets_fifo(queue):
    """Jobs for different targets are queued in insertion order."""
    await _enqueue(queue, "device1.yaml")
    await _enqueue(queue, "device2.yaml")

    j1 = await queue.claim_next("client-A")
    j2 = await queue.claim_next("client-B")

    assert j1.target == "device1.yaml"
    assert j2.target == "device2.yaml"


async def test_deduplication_same_target(queue):
    """A second enqueue for the same target while one is active returns None."""
    j1 = await _enqueue(queue, "device1.yaml")
    j2 = await _enqueue(queue, "device1.yaml")

    assert j1 is not None
    assert j2 is None  # duplicate rejected


async def test_deduplication_after_finish(queue):
    """A new job for the same target is allowed after the previous one finishes."""
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")
    await queue.submit_result(job.id, "success", log="ok")

    # Now we can enqueue again
    j2 = await _enqueue(queue, "device1.yaml")
    assert j2 is not None


# ---------------------------------------------------------------------------
# Job lifecycle transitions
# ---------------------------------------------------------------------------

async def test_claim_transitions_to_working(queue):
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")
    assert job.state == JobState.WORKING
    assert job.assigned_client_id == "client-A"
    assert job.assigned_at is not None


async def test_submit_result_success(queue):
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")
    await queue.submit_result(job.id, "success", log="built ok", ota_result="success")

    stored = queue.get(job.id)
    assert stored.state == JobState.SUCCESS
    assert stored.log == "built ok"
    assert stored.ota_result == "success"
    assert stored.finished_at is not None


async def test_submit_result_failed(queue):
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")
    await queue.submit_result(job.id, "failed", log="compile error")

    stored = queue.get(job.id)
    assert stored.state == JobState.FAILED


async def test_no_job_when_queue_empty(queue):
    result = await queue.claim_next("client-A")
    assert result is None


# ---------------------------------------------------------------------------
# Atomicity: concurrent claims
# ---------------------------------------------------------------------------

async def test_concurrent_claims_atomic(tmp_path):
    """Two coroutines racing to claim the same job: only one should win."""
    q = JobQueue(queue_file=tmp_path / "test_atomic_queue.json")
    await q.enqueue("device.yaml", "2024.3.1", "run1", 300)

    results = await asyncio.gather(
        q.claim_next("client-A"),
        q.claim_next("client-B"),
    )
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1, f"Expected exactly 1 claim, got {len(claimed)}"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

async def test_cancel_pending_job(queue):
    job = await _enqueue(queue, "device1.yaml")
    n = await queue.cancel([job.id])
    assert n == 1
    assert queue.get(job.id).state == JobState.CANCELLED


async def test_cancel_working_job(queue):
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")
    assert job.state == JobState.WORKING
    n = await queue.cancel([job.id])
    assert n == 1
    assert queue.get(job.id).state == JobState.CANCELLED


async def test_cancel_already_finished_job(queue):
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")
    await queue.submit_result(job.id, "success")
    n = await queue.cancel([job.id])
    assert n == 0  # already terminal


async def test_cancel_multiple_jobs(queue):
    j1 = await _enqueue(queue, "device1.yaml")
    j2 = await _enqueue(queue, "device2.yaml")
    n = await queue.cancel([j1.id, j2.id])
    assert n == 2


# ---------------------------------------------------------------------------
# Timeout and retry
# ---------------------------------------------------------------------------

async def _make_queue_with_old_job(tmp_queue_file) -> tuple[JobQueue, str]:
    """Create a queue with a job that was assigned far in the past."""
    q = JobQueue(queue_file=tmp_queue_file)
    await q.enqueue("device.yaml", "2024.3.1", "run1", timeout_seconds=1)
    claimed = await q.claim_next("client-A")
    # Backdate assigned_at so it's timed out
    claimed.assigned_at = _utcnow() - timedelta(seconds=10)
    return q, claimed.id


async def test_check_timeouts_requeues_job(tmp_queue_file):
    q, job_id = await _make_queue_with_old_job(tmp_queue_file)
    affected = await q.check_timeouts()
    assert len(affected) == 1
    stored = q.get(job_id)
    assert stored.state == JobState.PENDING
    assert stored.retry_count == 1
    assert stored.assigned_client_id is None


async def test_check_timeouts_max_retries(tmp_queue_file):
    q, job_id = await _make_queue_with_old_job(tmp_queue_file)

    # Simulate 3 consecutive timeouts
    for i in range(3):
        await q.check_timeouts()
        job = q.get(job_id)
        if job.state == JobState.FAILED:
            break
        # Re-assign and backdate again
        claimed = await q.claim_next("client-X")
        if claimed:
            claimed.assigned_at = _utcnow() - timedelta(seconds=10)

    stored = q.get(job_id)
    assert stored.state == JobState.FAILED
    assert stored.retry_count >= 3


async def test_check_timeouts_persists_before_recording_history(tmp_queue_file, monkeypatch):
    """PR #64 review: ordering invariant — the permanent-fail path must
    persist queue.json BEFORE calling _record_history. Otherwise a
    SIGKILL between the two leaves history ahead of queue, and load()
    re-enqueues a terminal job that gets a second (possibly
    different) terminal row on its re-run."""
    q, _job_id = await _make_queue_with_old_job(tmp_queue_file)

    call_order: list[str] = []
    real_persist = q._persist
    real_record = q._record_history

    def spy_persist() -> None:
        call_order.append("persist")
        real_persist()

    def spy_record(job) -> None:
        call_order.append(f"record:{job.state.value}")
        real_record(job)

    monkeypatch.setattr(q, "_persist", spy_persist)
    monkeypatch.setattr(q, "_record_history", spy_record)

    # Run check_timeouts until the job is permanently failed.
    for _ in range(MAX_RETRIES):
        await q.check_timeouts()
        # Re-assign + backdate so the next tick sees another timeout.
        claimed = await q.claim_next("client-X")
        if claimed:
            claimed.assigned_at = _utcnow() - timedelta(seconds=10)

    # Every ``record:failed`` must be preceded by at least one
    # ``persist`` in the same invocation. Easier assertion: the
    # LAST persist call must come before (or at) the LAST
    # record:failed call.
    last_persist = max(i for i, c in enumerate(call_order) if c == "persist")
    last_failed_record = max(
        (i for i, c in enumerate(call_order) if c == "record:failed"),
        default=-1,
    )
    assert last_failed_record > last_persist, (
        f"history write must happen after persist; got call_order={call_order}"
    )


async def test_check_timeouts_no_false_positives(tmp_queue_file):
    """A recently-assigned job should NOT be timed out."""
    q = JobQueue(queue_file=tmp_queue_file)
    await q.enqueue("device.yaml", "2024.3.1", "run1", timeout_seconds=9999)
    await q.claim_next("client-A")
    affected = await q.check_timeouts()
    assert len(affected) == 0


async def test_check_timeouts_requeues_when_worker_offline(tmp_queue_file):
    """Bug #17: a WORKING job whose worker is offline should be re-queued
    immediately, not wait out the full JOB_TIMEOUT."""
    q = JobQueue(queue_file=tmp_queue_file)
    await q.enqueue("device.yaml", "2024.3.1", "run1", timeout_seconds=9999)
    claimed = await q.claim_next("sleepy-worker")
    assert claimed is not None
    # Simulate: worker went offline (registry reports not-online), but the
    # job isn't remotely close to its JOB_TIMEOUT yet.
    affected = await q.check_timeouts(is_worker_online=lambda cid: False)
    assert len(affected) == 1, "offline-worker job must be short-circuited"
    stored = q.get(claimed.id)
    assert stored.state == JobState.PENDING
    assert stored.assigned_client_id is None
    assert stored.retry_count == 1


async def test_check_timeouts_leaves_job_alone_when_worker_online(tmp_queue_file):
    """Bug #17: the liveness check must not kick in while the worker is
    still online and the deadline hasn't passed."""
    q = JobQueue(queue_file=tmp_queue_file)
    await q.enqueue("device.yaml", "2024.3.1", "run1", timeout_seconds=9999)
    claimed = await q.claim_next("live-worker")
    assert claimed is not None
    affected = await q.check_timeouts(is_worker_online=lambda cid: True)
    assert len(affected) == 0
    assert q.get(claimed.id).state == JobState.WORKING


async def test_check_timeouts_offline_worker_respects_max_retries(tmp_queue_file):
    """Bug #17: after MAX_RETRIES offline re-queues the job must still
    transition to FAILED permanently (not requeue forever)."""
    q = JobQueue(queue_file=tmp_queue_file)
    await q.enqueue("device.yaml", "2024.3.1", "run1", timeout_seconds=9999)

    final_state = None
    for _ in range(5):
        claimed = await q.claim_next("offline-worker")
        if claimed is None:
            break
        await q.check_timeouts(is_worker_online=lambda cid: False)
        stored = q.get(claimed.id)
        if stored.state == JobState.FAILED:
            final_state = stored
            break

    assert final_state is not None, "job should eventually transition to FAILED"
    assert "offline-worker requeues" in (final_state.log or "")


# ---------------------------------------------------------------------------
# Persistence / restart recovery
# ---------------------------------------------------------------------------

async def test_persistence_pending_jobs_reload(tmp_queue_file):
    q1 = JobQueue(queue_file=tmp_queue_file)
    await _enqueue(q1, "device1.yaml")
    await _enqueue(q1, "device2.yaml")

    # Simulate restart
    q2 = JobQueue(queue_file=tmp_queue_file)
    q2.load()

    jobs = q2.get_all()
    assert len(jobs) == 2
    assert all(j.state == JobState.PENDING for j in jobs)


async def test_persistence_working_resets_to_pending(tmp_queue_file):
    q1 = JobQueue(queue_file=tmp_queue_file)
    await _enqueue(q1, "device1.yaml")
    job = await q1.claim_next("client-A")
    assert job.state == JobState.WORKING

    # Simulate restart
    q2 = JobQueue(queue_file=tmp_queue_file)
    q2.load()

    jobs = q2.get_all()
    assert len(jobs) == 1
    assert jobs[0].state == JobState.PENDING
    assert jobs[0].assigned_client_id is None


async def test_persistence_interrupted_server_ota_marked_failed(tmp_queue_file):
    """SOTA.2: a server_ota job whose OTA push was still running at shutdown
    must not come back as a permanently-spinning row.

    The push is an in-process asyncio task with no persisted progress, and
    the job is already terminal (SUCCESS), so neither the WORKING->PENDING
    reset nor timeout_checker would ever revisit it. The UI treats
    success + ota_result=None as in-flight, so left alone it spins forever.
    """
    q1 = JobQueue(queue_file=tmp_queue_file)
    await _enqueue(q1, "thread-device.yaml", server_ota=True)
    job = await q1.claim_next("client-A")
    # Worker compiled only; the server OTA push had not reported back yet.
    await q1.submit_result(job.id, "success", log=None, ota_result=None)
    mid = q1.get(job.id)
    assert mid.state == JobState.SUCCESS
    assert mid.ota_result is None

    # Simulate restart
    q2 = JobQueue(queue_file=tmp_queue_file)
    q2.load()

    recovered = q2.get_all()[0]
    assert recovered.state == JobState.SUCCESS, "compile genuinely did succeed"
    assert recovered.ota_result == "failed", (
        "an OTA push lost to a restart must surface as failed, not as "
        "an in-flight job that never resolves"
    )
    assert recovered.status_text is None


async def test_persistence_completed_server_ota_untouched(tmp_queue_file):
    """Complement: a server_ota job whose push already finished must be
    left exactly as it was — the recovery rule keys on ota_result being
    unset, not on server_ota alone."""
    q1 = JobQueue(queue_file=tmp_queue_file)
    await _enqueue(q1, "thread-device.yaml", server_ota=True)
    job = await q1.claim_next("client-A")
    await q1.submit_result(job.id, "success", log=None, ota_result=None)
    await q1.patch_ota_result(job.id, "success")

    q2 = JobQueue(queue_file=tmp_queue_file)
    q2.load()

    assert q2.get_all()[0].ota_result == "success"


async def test_persistence_non_server_ota_success_untouched(tmp_queue_file):
    """A plain download-only job is terminal on success with no OTA phase;
    the recovery rule must not invent a failed OTA for it."""
    q1 = JobQueue(queue_file=tmp_queue_file)
    await _enqueue(q1, "device1.yaml", download_only=True)
    job = await q1.claim_next("client-A")
    await q1.submit_result(job.id, "success", log=None, ota_result=None)

    q2 = JobQueue(queue_file=tmp_queue_file)
    q2.load()

    assert q2.get_all()[0].ota_result is None


def test_persistence_backwards_compat_old_states(tmp_queue_file):
    """Old queue.json with 'assigned'/'running' state values load as WORKING then reset to PENDING."""
    old_data = [
        {
            "id": "aaaa-1111",
            "target": "device1.yaml",
            "esphome_version": "2024.3.1",
            "state": "assigned",
            "run_id": "run1",
            "assigned_client_id": "client-A",
            "assigned_hostname": "myhost",
            "assigned_at": "2024-01-01T00:00:00+00:00",
            "worker_id": 1,
            "timeout_seconds": 600,
            "created_at": "2024-01-01T00:00:00+00:00",
            "finished_at": None,
            "retry_count": 0,
            "log": None,
            "ota_result": None,
            "ota_only": False,
            "pinned_client_id": None,
            "status_text": None,
            "duration_seconds": None,
        },
        {
            "id": "bbbb-2222",
            "target": "device2.yaml",
            "esphome_version": "2024.3.1",
            "state": "running",
            "run_id": "run1",
            "assigned_client_id": "client-B",
            "assigned_hostname": "myhost2",
            "assigned_at": "2024-01-01T00:00:00+00:00",
            "worker_id": 1,
            "timeout_seconds": 600,
            "created_at": "2024-01-01T00:00:00+00:00",
            "finished_at": None,
            "retry_count": 0,
            "log": None,
            "ota_result": None,
            "ota_only": False,
            "pinned_client_id": None,
            "status_text": None,
            "duration_seconds": None,
        },
    ]
    tmp_queue_file.write_text(json.dumps(old_data))

    q = JobQueue(queue_file=tmp_queue_file)
    q.load()

    jobs = {j.id: j for j in q.get_all()}
    assert jobs["aaaa-1111"].state == JobState.PENDING
    assert jobs["aaaa-1111"].assigned_client_id is None
    assert jobs["bbbb-2222"].state == JobState.PENDING
    assert jobs["bbbb-2222"].assigned_client_id is None


async def test_persistence_success_retained(tmp_queue_file):
    q1 = JobQueue(queue_file=tmp_queue_file)
    await _enqueue(q1, "device1.yaml")
    job = await q1.claim_next("client-A")
    await q1.submit_result(job.id, "success", log="ok")

    q2 = JobQueue(queue_file=tmp_queue_file)
    q2.load()

    jobs = q2.get_all()
    assert len(jobs) == 1
    assert jobs[0].state == JobState.SUCCESS


async def test_persistence_failed_retained(tmp_queue_file):
    q1 = JobQueue(queue_file=tmp_queue_file)
    await _enqueue(q1, "device1.yaml")
    job = await q1.claim_next("client-A")
    await q1.submit_result(job.id, "failed", log="error")

    q2 = JobQueue(queue_file=tmp_queue_file)
    q2.load()

    jobs = q2.get_all()
    assert jobs[0].state == JobState.FAILED


async def test_persistence_atomic_write(tmp_queue_file):
    """The queue file should be valid JSON after every enqueue."""
    q = JobQueue(queue_file=tmp_queue_file)
    for i in range(5):
        await _enqueue(q, f"device{i}.yaml")

    data = json.loads(tmp_queue_file.read_text())
    assert isinstance(data, list)
    assert len(data) == 5


# ---------------------------------------------------------------------------
# Queue size
# ---------------------------------------------------------------------------

async def test_queue_size(queue):
    assert queue.queue_size() == 0
    await _enqueue(queue, "d1.yaml")
    await _enqueue(queue, "d2.yaml")
    assert queue.queue_size() == 2

    job = await queue.claim_next("client-A")
    await queue.submit_result(job.id, "success")
    assert queue.queue_size() == 1


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

async def test_retry_failed_job_creates_pending(queue):
    await _enqueue(queue, "device1.yaml")
    claimed = await queue.claim_next("client-A")
    await queue.submit_result(claimed.id, "failed", log="error")

    new_jobs = await queue.retry([claimed.id], "2024.3.1", "run2", 300)
    assert len(new_jobs) == 1
    assert new_jobs[0].state == JobState.PENDING
    assert new_jobs[0].target == "device1.yaml"
    assert new_jobs[0].id != claimed.id  # new job, new id


async def test_retry_timed_out_job_creates_pending(tmp_queue_file):
    q, job_id = await _make_queue_with_old_job(tmp_queue_file)
    await q.check_timeouts()  # transitions to TIMED_OUT or PENDING after retry=0

    # After first timeout the job is back to PENDING with retry_count=1;
    # force it to TIMED_OUT by exhausting retries
    for _ in range(3):
        timed_out = q.get(job_id)
        if timed_out.state == JobState.FAILED:
            break
        claimed = await q.claim_next("x")
        if claimed:
            claimed.assigned_at = _utcnow() - timedelta(seconds=10)
        await q.check_timeouts()

    # Now retry via the retry() method
    timed_out_job = next(
        (j for j in q.get_all() if j.state in (JobState.FAILED, JobState.TIMED_OUT)),
        None,
    )
    if timed_out_job:
        new_jobs = await q.retry([timed_out_job.id], "2024.3.1", "run2", 300)
        assert len(new_jobs) == 1
        assert new_jobs[0].state == JobState.PENDING


async def test_retry_ignores_non_terminal_jobs(queue):
    job = await _enqueue(queue, "device1.yaml")
    await queue.claim_next("client-A")  # now WORKING

    new_jobs = await queue.retry([job.id], "2024.3.1", "run2", 300)
    assert new_jobs == []  # WORKING job is not retryable


async def test_retry_success_jobs(queue):
    """Success jobs can be individually retried (e.g. config changed after build)."""
    await _enqueue(queue, "device1.yaml")
    claimed = await queue.claim_next("client-A")
    await queue.submit_result(claimed.id, "success")

    new_jobs = await queue.retry([claimed.id], "2024.3.1", "run2", 300)
    assert len(new_jobs) == 1
    assert new_jobs[0].target == "device1.yaml"
    assert new_jobs[0].state.value == "pending"


async def test_retry_multiple_jobs(queue):
    j1 = await _enqueue(queue, "d1.yaml")
    j2 = await _enqueue(queue, "d2.yaml")
    c1 = await queue.claim_next("A")
    c2 = await queue.claim_next("B")
    await queue.submit_result(c1.id, "failed")
    await queue.submit_result(c2.id, "failed")

    new_jobs = await queue.retry([c1.id, c2.id], "2024.3.1", "run2", 300)
    assert len(new_jobs) == 2
    targets = {j.target for j in new_jobs}
    assert targets == {"d1.yaml", "d2.yaml"}


# ---------------------------------------------------------------------------
# Bounded log storage (SEC.2)
# ---------------------------------------------------------------------------

async def test_append_log_basic(queue):
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")
    ok = await queue.append_log(job.id, "line 1\n")
    assert ok
    assert queue.get(job.id)._streaming_log == "line 1\n"


async def test_append_log_unknown_job(queue):
    ok = await queue.append_log("nonexistent", "text")
    assert not ok


async def test_append_log_truncates_at_max(queue):
    """Log exceeding MAX_LOG_BYTES is truncated with a marker."""
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")

    # Write just under the limit, then push over
    chunk = "x" * (MAX_LOG_BYTES - 10)
    await queue.append_log(job.id, chunk)
    await queue.append_log(job.id, "y" * 100)  # pushes over

    log = queue.get(job.id)._streaming_log
    assert log.endswith(LOG_TRUNCATED_MARKER)
    # Total never exceeds the configured cap (marker is reserved within it)
    assert len(log) == MAX_LOG_BYTES


async def test_append_log_drops_after_truncation(queue):
    """After truncation, further appends are silently dropped."""
    await _enqueue(queue, "device1.yaml")
    job = await queue.claim_next("client-A")

    # Fill past max to trigger truncation
    await queue.append_log(job.id, "x" * (MAX_LOG_BYTES + 1))
    log_after_truncate = queue.get(job.id)._streaming_log
    assert LOG_TRUNCATED_MARKER in log_after_truncate

    # Further appends should not change the log
    await queue.append_log(job.id, "more data")
    assert queue.get(job.id)._streaming_log == log_after_truncate


# ---------------------------------------------------------------------------
# Pinned jobs — pinned_client_id semantics
# ---------------------------------------------------------------------------

async def test_pinned_job_stored_on_enqueue(queue):
    job = await queue.enqueue(
        "device.yaml", "2024.3.1", "run1", 300,
        pinned_client_id="worker-42",
    )
    assert job is not None
    assert job.pinned_client_id == "worker-42"


async def test_pinned_job_only_claimable_by_pinned_worker(queue):
    await queue.enqueue(
        "device.yaml", "2024.3.1", "run1", 300,
        pinned_client_id="worker-42",
    )

    # Other worker can't claim
    job = await queue.claim_next("worker-other")
    assert job is None

    # Pinned worker can
    job = await queue.claim_next("worker-42")
    assert job is not None
    assert job.assigned_client_id == "worker-42"


async def test_unpinned_job_claimable_by_any_worker(queue):
    await _enqueue(queue, "device.yaml")
    job = await queue.claim_next("any-worker")
    assert job is not None


async def test_retry_preserves_pinned_client_id(queue):
    """All retried jobs (not just OTA retries) must keep their original pin."""
    await queue.enqueue(
        "device.yaml", "2024.3.1", "run1", 300,
        pinned_client_id="worker-42",
    )
    claimed = await queue.claim_next("worker-42")
    await queue.submit_result(claimed.id, "failed", log="error")

    new_jobs = await queue.retry([claimed.id], "2024.3.1", "run2", 300)
    assert len(new_jobs) == 1
    assert new_jobs[0].pinned_client_id == "worker-42"


# ---------------------------------------------------------------------------
# OTA-only retry — submit ota_result preserves compile success
# ---------------------------------------------------------------------------

async def test_submit_result_ota_only(queue):
    """A job with ota_only=True represents a re-upload of an already-compiled target."""
    await queue.enqueue("device.yaml", "2024.3.1", "run1", 300)
    claimed = await queue.claim_next("worker")
    await queue.submit_result(claimed.id, "success", log="ok", ota_result="success")

    stored = queue.get(claimed.id)
    assert stored.state == JobState.SUCCESS
    assert stored.ota_result == "success"


async def test_submit_result_compile_success_but_ota_failed(queue):
    """Compile succeeded but OTA failed → state=SUCCESS, ota_result=failed."""
    await queue.enqueue("device.yaml", "2024.3.1", "run1", 300)
    claimed = await queue.claim_next("worker")
    await queue.submit_result(claimed.id, "success", log="ok", ota_result="failed")

    stored = queue.get(claimed.id)
    assert stored.state == JobState.SUCCESS
    assert stored.ota_result == "failed"


# ---------------------------------------------------------------------------
# Status text + claim race detection
# ---------------------------------------------------------------------------

async def test_update_status_sets_status_text(queue):
    """update_status is used by workers to report phase changes (e.g. 'Compiling')."""
    await _enqueue(queue, "device.yaml")
    claimed = await queue.claim_next("worker")

    await queue.update_status(claimed.id, "Compiling + OTA")
    stored = queue.get(claimed.id)
    assert stored.status_text == "Compiling + OTA"

    await queue.update_status(claimed.id, "OTA Retry")
    stored = queue.get(claimed.id)
    assert stored.status_text == "OTA Retry"


async def test_update_status_unknown_job_is_noop(queue):
    """Updating a nonexistent job must not raise."""
    # Should not raise — the return value depends on the implementation
    await queue.update_status("nonexistent-id", "something")


# ---------------------------------------------------------------------------
# finished_at is set on terminal transitions
# ---------------------------------------------------------------------------

async def test_finished_at_set_on_success(queue):
    await _enqueue(queue, "d.yaml")
    claimed = await queue.claim_next("w")
    await queue.submit_result(claimed.id, "success")
    assert queue.get(claimed.id).finished_at is not None


async def test_finished_at_set_on_failure(queue):
    await _enqueue(queue, "d.yaml")
    claimed = await queue.claim_next("w")
    await queue.submit_result(claimed.id, "failed")
    assert queue.get(claimed.id).finished_at is not None


async def test_finished_at_unset_on_pending(queue):
    await _enqueue(queue, "d.yaml")
    job = await _enqueue(queue, "d.yaml")  # deduped — returns None
    assert job is None
    stored = queue.get_all()[0]
    assert stored.finished_at is None


# ---------------------------------------------------------------------------
# B.3 — timeout_checker frozen-clock behavior documentation
# ---------------------------------------------------------------------------

async def test_check_timeouts_behavior_is_purely_deadline_based(tmp_queue_file):
    """Frozen-clock test documenting the three B.3 cases in one place:

    (a) WORKING job past deadline → PENDING, retry_count++.
    (b) After MAX_RETRIES (3) timeouts → FAILED permanently.
    (c) Heartbeats do NOT affect job timeout — the check is purely a
        comparison against ``job.assigned_at + timeout_seconds``. A heartbeat
        arriving during the timeout window does not reset the deadline;
        worker liveness is the registry's concern, not the queue's. This test
        pins that contract so nobody "fixes" it by coupling the two.
    """
    q = JobQueue(queue_file=tmp_queue_file)
    await q.enqueue("dev.yaml", "2024.3.1", "run1", timeout_seconds=1)
    claimed = await q.claim_next("worker-a")
    assert claimed is not None

    # (a) Backdate deadline into the past → expect requeue.
    claimed.assigned_at = _utcnow() - timedelta(seconds=10)
    affected = await q.check_timeouts()
    assert [j.id for j in affected] == [claimed.id]
    job = q.get(claimed.id)
    assert job.state == JobState.PENDING
    assert job.retry_count == 1
    assert job.assigned_client_id is None

    # (b) Drive it to the failure cap.
    for _ in range(MAX_RETRIES + 1):
        await q.check_timeouts()
        if q.get(claimed.id).state == JobState.FAILED:
            break
        rec = await q.claim_next("worker-a")
        if rec is not None:
            rec.assigned_at = _utcnow() - timedelta(seconds=10)
    job = q.get(claimed.id)
    assert job.state == JobState.FAILED
    assert "Permanently failed" in (job.log or "")

    # (c) A fresh assignment with a future deadline must not be touched even
    # though there is no heartbeat tracking in the queue at all.
    await q.enqueue("other.yaml", "2024.3.1", "run1", timeout_seconds=9999)
    fresh = await q.claim_next("worker-b")
    affected = await q.check_timeouts()
    assert fresh.id not in [j.id for j in affected]


# ---------------------------------------------------------------------------
# B.6 — queue.json corruption tests
#
# The queue MUST recover gracefully from a corrupt persistence file rather
# than crashing the whole server at startup. Both a truncated file (invalid
# JSON) and a structurally-valid-but-semantically-broken file should be
# detected, logged at ERROR level, and fall back to an empty queue.
# ---------------------------------------------------------------------------

async def test_load_malformed_json_logs_error_and_starts_empty(tmp_queue_file, caplog):
    """Invalid JSON in queue.json must not crash the server — it must log an
    error and start with an empty in-memory queue."""
    import logging

    # Write non-JSON garbage.
    Path(tmp_queue_file).write_text("not valid json {[")

    q = JobQueue(queue_file=Path(tmp_queue_file))
    with caplog.at_level(logging.ERROR, logger="job_queue"):
        q.load()

    assert q.get_all() == []
    errors = [r for r in caplog.records if r.name == "job_queue" and r.levelno >= logging.ERROR]
    assert errors, f"expected ERROR-level log record, got: {[r.getMessage() for r in caplog.records]}"
    assert any("load queue" in r.getMessage().lower() for r in errors)


async def test_load_truncated_json_logs_error_and_starts_empty(tmp_queue_file, caplog):
    """A truncated persistence file (e.g. crash mid-write) must not crash."""
    import logging

    # Simulate a truncated write: valid prefix, cut off mid-object.
    Path(tmp_queue_file).write_text('[{"id": "abc", "target": "dev.yaml"')

    q = JobQueue(queue_file=Path(tmp_queue_file))
    with caplog.at_level(logging.ERROR, logger="job_queue"):
        q.load()

    assert q.get_all() == []
    assert any(r.levelno >= logging.ERROR for r in caplog.records if r.name == "job_queue")


async def test_load_recovers_from_partial_corruption(tmp_queue_file, caplog):
    """If the JSON parses but one job entry is missing required fields, the
    load must skip the bad entry and preserve valid entries (rather than
    dropping the whole file or crashing the server).
    """
    import logging

    good = {
        "id": "good-1",
        "target": "dev.yaml",
        "esphome_version": "2024.3.1",
        "state": "pending",
        "run_id": "run1",
        "retry_count": 0,
        "timeout_seconds": 600,
        "created_at": _utcnow().isoformat(),
    }
    bad = {"id": "bad", "not_a_job": True}  # missing required fields
    Path(tmp_queue_file).write_text(json.dumps([good, bad, good]))

    q = JobQueue(queue_file=Path(tmp_queue_file))
    with caplog.at_level(logging.ERROR, logger="job_queue"):
        q.load()

    loaded = {j.id for j in q.get_all()}
    assert "good-1" in loaded, (
        f"valid jobs must survive a partially-corrupt file; loaded={loaded}"
    )
    # An error should be logged for the bad entry.
    assert any(r.levelno >= logging.ERROR for r in caplog.records if r.name == "job_queue")


# ---------------------------------------------------------------------------
# #23 — coalesced follow-up enqueue rules
# ---------------------------------------------------------------------------

async def test_enqueue_coalesces_followup_while_working(tmp_queue_file):
    """A second enqueue while the first job is WORKING creates a follow-up
    (PENDING, is_followup=True). The follow-up is not claimable while the
    first job is still WORKING."""
    q = JobQueue(queue_file=Path(tmp_queue_file))
    j1 = await q.enqueue("dev.yaml", "2024.3.1", "run1", timeout_seconds=600)
    assert j1 is not None and not j1.is_followup
    claimed = await q.claim_next("worker-a")
    assert claimed is not None and claimed.id == j1.id
    assert claimed.state == JobState.WORKING

    # Second enqueue while j1 is WORKING → creates a follow-up.
    j2 = await q.enqueue("dev.yaml", "2024.3.1", "run2", timeout_seconds=600)
    assert j2 is not None
    assert j2.id != j1.id
    assert j2.is_followup is True
    assert j2.state == JobState.PENDING

    # claim_next must NOT pick the follow-up while j1 is still WORKING.
    blocked = await q.claim_next("worker-b")
    assert blocked is None

    # Once j1 finishes, the follow-up becomes eligible.
    await q.submit_result(j1.id, "success")
    next_job = await q.claim_next("worker-b")
    assert next_job is not None and next_job.id == j2.id
    assert next_job.state == JobState.WORKING
    # is_followup is cleared once it's claimed — it's no longer "queued behind".
    assert next_job.is_followup is False


async def test_enqueue_no_followup_for_pending(tmp_queue_file):
    """A second enqueue while the first is still PENDING (not yet claimed)
    is a no-op — the user's edits will be picked up at claim time."""
    q = JobQueue(queue_file=Path(tmp_queue_file))
    j1 = await q.enqueue("dev.yaml", "2024.3.1", "run1", timeout_seconds=600)
    assert j1 is not None
    j2 = await q.enqueue("dev.yaml", "2024.3.1", "run2", timeout_seconds=600)
    assert j2 is None  # no-op
    assert len(q.get_all()) == 1


async def test_enqueue_updates_existing_followup(tmp_queue_file):
    """A third enqueue while one is WORKING and one follow-up exists
    UPDATES the follow-up rather than creating a second follow-up."""
    q = JobQueue(queue_file=Path(tmp_queue_file))
    j1 = await q.enqueue("dev.yaml", "2024.3.1", "run1", timeout_seconds=600)
    assert j1 is not None
    await q.claim_next("worker-a")  # j1 → WORKING

    j2 = await q.enqueue("dev.yaml", "2024.3.1", "run2", timeout_seconds=600, pinned_client_id="worker-a")
    assert j2 is not None and j2.is_followup
    assert j2.pinned_client_id == "worker-a"

    # Third enqueue with different params should UPDATE j2, not create j3.
    j3 = await q.enqueue("dev.yaml", "2025.1.0", "run3", timeout_seconds=600, pinned_client_id="worker-b")
    assert j3 is not None
    assert j3.id == j2.id  # same job, updated in place
    assert j3.esphome_version == "2025.1.0"
    assert j3.pinned_client_id == "worker-b"
    assert j3.run_id == "run3"
    # Still exactly 2 jobs total.
    assert len([x for x in q.get_all() if x.state in (JobState.PENDING, JobState.WORKING)]) == 2


async def test_followup_does_not_block_other_targets(tmp_queue_file):
    """A WORKING job for target A must not block claiming a PENDING job
    for target B (only the same-target follow-ups are blocked)."""
    q = JobQueue(queue_file=Path(tmp_queue_file))
    a1 = await q.enqueue("a.yaml", "2024.3.1", "run1", timeout_seconds=600)
    await q.claim_next("worker-a")  # a1 → WORKING
    b1 = await q.enqueue("b.yaml", "2024.3.1", "run1", timeout_seconds=600)
    assert b1 is not None and not b1.is_followup
    next_b = await q.claim_next("worker-b")
    assert next_b is not None and next_b.id == b1.id
    assert a1 is not None  # silence type-checker — only used for setup


async def test_validate_only_jobs_bypass_coalescing(tmp_queue_file):
    """Validate-only jobs are independent and should not be turned into
    follow-ups even when a normal compile is WORKING for the same target."""
    q = JobQueue(queue_file=Path(tmp_queue_file))
    compile_job = await q.enqueue("dev.yaml", "2024.3.1", "run1", timeout_seconds=600)
    await q.claim_next("worker-a")
    validate_job = await q.enqueue("dev.yaml", "2024.3.1", "run2", timeout_seconds=60, validate_only=True)
    assert validate_job is not None
    assert validate_job.is_followup is False
    assert validate_job.validate_only is True
    # Validate job is immediately claimable — it doesn't depend on compile completing.
    next_job = await q.claim_next("worker-b")
    assert next_job is not None and next_job.id == validate_job.id
    assert compile_job is not None  # silence


# ---------------------------------------------------------------------------
# FD.1/FD.7 — download-only flag + firmware lifecycle
# ---------------------------------------------------------------------------

async def test_enqueue_records_download_only_flag(tmp_queue_file):
    q = JobQueue(queue_file=tmp_queue_file)
    job = await q.enqueue(
        target="device.yaml",
        esphome_version="2026.3.2",
        run_id="run1",
        timeout_seconds=300,
        download_only=True,
    )
    assert job is not None
    assert job.download_only is True
    assert job.has_firmware is False


async def test_download_only_flag_persists_across_reload(tmp_queue_file):
    q = JobQueue(queue_file=tmp_queue_file)
    job = await q.enqueue("device.yaml", "2026.3.2", "run1", 300, download_only=True)
    assert job is not None
    q2 = JobQueue(queue_file=tmp_queue_file)
    q2.load()
    roundtrip = q2.get(job.id)
    assert roundtrip is not None
    assert roundtrip.download_only is True
    assert roundtrip.has_firmware is False


async def test_mark_firmware_stored_requires_working_state(tmp_queue_file):
    q = JobQueue(queue_file=tmp_queue_file)
    job = await q.enqueue("device.yaml", "2026.3.2", "run1", 300, download_only=True)
    assert job is not None
    # PENDING → rejected.
    ok = await q.mark_firmware_stored(job.id)
    assert ok is False
    assert q.get(job.id).has_firmware is False

    # WORKING → accepted.
    await q.claim_next("worker-A")
    ok = await q.mark_firmware_stored(job.id)
    assert ok is True
    assert q.get(job.id).has_firmware is True


async def test_mark_firmware_stored_accepts_non_download_only_job(tmp_queue_file):
    """Bug #9 (1.6.1): OTA jobs also archive their compiled binary on
    the server. The old contract refused the flag flip for
    ``download_only=False`` so the budget enforcer never saw a
    protectable row; the new contract accepts it and the enforcer
    owns LRU eviction when disk pressure exceeds the cap."""
    q = JobQueue(queue_file=tmp_queue_file)
    job = await q.enqueue("device.yaml", "2026.3.2", "run1", 300, download_only=False)
    assert job is not None
    await q.claim_next("worker-A")
    ok = await q.mark_firmware_stored(job.id)
    assert ok is True


async def test_remove_jobs_preserves_download_only_firmware(tmp_queue_file, tmp_path, monkeypatch):
    """Bug #38: download-only firmware must survive user Remove. The
    binary stays on disk until the firmware_budget_enforcer evicts it
    (space pressure) or the user explicitly deletes the history row.
    Pre-#38 behavior purged it eagerly — this test pins the new
    contract so a future refactor can't silently bring that back."""
    from firmware_storage import save_firmware, firmware_path
    import firmware_storage

    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    q = JobQueue(queue_file=tmp_queue_file)
    job = await q.enqueue("device.yaml", "2026.3.2", "run1", 300, download_only=True)
    assert job is not None
    await q.claim_next("worker-A")
    save_firmware(job.id, b"fw", root=firmware_dir)
    await q.mark_firmware_stored(job.id)

    # Transition to SUCCESS so remove_jobs will accept it.
    await q.submit_result(job.id, "success", log=None, ota_result=None)
    assert firmware_path(job.id, root=firmware_dir).is_file()

    removed = await q.remove_jobs([job.id])
    assert removed == 1
    # Bug #38: retained.
    assert firmware_path(job.id, root=firmware_dir).exists()


async def test_clear_preserves_download_only_firmware(tmp_queue_file, tmp_path, monkeypatch):
    """Bug #38: user Clear keeps download-only firmware around."""
    from firmware_storage import save_firmware, firmware_path
    import firmware_storage

    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    q = JobQueue(queue_file=tmp_queue_file)
    job = await q.enqueue("device.yaml", "2026.3.2", "run1", 300, download_only=True)
    assert job is not None
    await q.claim_next("worker-A")
    save_firmware(job.id, b"fw", root=firmware_dir)
    await q.mark_firmware_stored(job.id)
    await q.submit_result(job.id, "success", log=None, ota_result=None)

    removed = await q.clear(["success"])
    assert removed == 1
    assert firmware_path(job.id, root=firmware_dir).exists()


async def test_per_target_coalescing_preserves_download_only_firmware(tmp_queue_file, tmp_path, monkeypatch):
    """Bug #38: per-target enqueue coalescing evicts the old queue
    entry but keeps its download-only firmware on disk."""
    from firmware_storage import save_firmware, firmware_path
    import firmware_storage

    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    q = JobQueue(queue_file=tmp_queue_file)
    first = await q.enqueue("device.yaml", "2026.3.2", "run1", 300, download_only=True)
    assert first is not None
    await q.claim_next("worker-A")
    save_firmware(first.id, b"old", root=firmware_dir)
    await q.mark_firmware_stored(first.id)
    await q.submit_result(first.id, "success", log=None, ota_result=None)
    assert firmware_path(first.id, root=firmware_dir).is_file()

    second = await q.enqueue("device.yaml", "2026.3.2", "run2", 300, download_only=True)
    assert second is not None
    assert q.get(first.id) is None  # queue entry evicted
    assert firmware_path(first.id, root=firmware_dir).exists()


async def test_remove_jobs_preserves_ota_firmware_when_archived(tmp_queue_file, tmp_path, monkeypatch):
    """Bug #9 (1.6.1): the retention exemption now covers any job
    whose firmware was actually archived. Pre-1.6.1 an OTA job's
    ``.bin`` was eagerly purged on Remove (even if ``has_firmware``
    was set); the new contract keeps it around so users can
    hand-flash or rollback after the OTA ran.

    The old test (``test_remove_jobs_deletes_non_download_firmware``)
    asserted the opposite — kept here with the inverted assertion so
    future refactors can't silently bring the eager purge back."""
    from firmware_storage import save_firmware, firmware_path
    import firmware_storage

    firmware_dir = tmp_path / "firmware"
    monkeypatch.setattr(firmware_storage, "DEFAULT_FIRMWARE_DIR", firmware_dir)

    q = JobQueue(queue_file=tmp_queue_file)
    # OTA path — download_only=False but firmware was archived.
    job = await q.enqueue("device.yaml", "2026.3.2", "run1", 300)
    assert job is not None
    await q.claim_next("worker-A")
    save_firmware(job.id, b"fw", root=firmware_dir)
    await q.mark_firmware_stored(job.id)
    await q.submit_result(job.id, "success", log=None, ota_result="success")
    assert firmware_path(job.id, root=firmware_dir).is_file()

    removed = await q.remove_jobs([job.id])
    assert removed == 1
    # Bug #9: retained — budget enforcer decides when to evict.
    assert firmware_path(job.id, root=firmware_dir).exists()
