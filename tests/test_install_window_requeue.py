"""#247 — a compile dispatched during the ESPHome install window must not
be blamed on the user's YAML.

Bundling imports the server's own lazy-installed ESPHome, so a job claimed
in the 1–3 minute first-boot / version-change install window fails with
``ModuleNotFoundError: No module named 'esphome'``. The old handler answered
every bundle failure with *"Fix the YAML error above and re-queue"*, sending
the user to edit a config that was never wrong.

That window is not exotic: it opens whenever the pinned ESPHome version
changes (one click in the UI) and whenever upstream publishes a release. It
hit the release smoke twice in the 1.7.3 → 1.8 cycle.

These tests cover the decision, not the aiohttp plumbing — the handler
itself lives behind the test client, which can't open sockets in this
sandbox, so the pieces it composes are tested directly.
"""

from __future__ import annotations

import asyncio

import pytest

from job_queue import JobQueue, JobState, MAX_RETRIES


# ---------------------------------------------------------------------------
# scanner: telling "still installing" from "tried and failed"
# ---------------------------------------------------------------------------

def test_install_in_flight_true_before_ready(monkeypatch):
    import scanner

    monkeypatch.setattr(scanner, "_esphome_install_failed", False)
    scanner._esphome_ready.clear()
    try:
        assert scanner.esphome_install_in_flight() is True
    finally:
        scanner._esphome_ready.set()


def test_install_in_flight_false_once_ready(monkeypatch):
    import scanner

    monkeypatch.setattr(scanner, "_esphome_install_failed", False)
    scanner._esphome_ready.set()
    assert scanner.esphome_install_in_flight() is False


def test_install_in_flight_false_after_a_failed_install(monkeypatch):
    """The distinction that matters: ``_esphome_ready`` is unset both while
    installing *and* after a failed install. Only the first is transient, so
    a failed install must not be retried as though it were."""
    import scanner

    monkeypatch.setattr(scanner, "_esphome_install_failed", True)
    scanner._esphome_ready.clear()
    try:
        assert scanner.esphome_install_in_flight() is False
    finally:
        scanner._esphome_ready.set()


# ---------------------------------------------------------------------------
# scanner: recognising the error through the bundle subprocess wrapper
# ---------------------------------------------------------------------------

def test_import_error_recognised_through_runtime_error_wrapper():
    """The real shape from the live repro — the bundle runs in a subprocess,
    so the ModuleNotFoundError arrives as stderr text inside a RuntimeError,
    not as a catchable exception type."""
    import scanner

    exc = RuntimeError(
        "bundle subprocess exited 1: Traceback (most recent call last):\n"
        "  File \"<string>\", line 3, in <module>\n"
        "ModuleNotFoundError: No module named 'esphome'\n"
    )
    assert scanner.esphome_import_error(exc) is True


def test_import_error_recognised_when_raised_directly():
    import scanner

    assert scanner.esphome_import_error(
        ModuleNotFoundError("No module named 'esphome'")
    ) is True


def test_a_real_yaml_error_is_not_an_import_error():
    """The guard rail. A genuine validation failure during the install window
    must keep the YAML wording — treating every failure in that window as
    transient would swallow real config errors."""
    import scanner

    exc = RuntimeError(
        "bundle subprocess exited 1: Invalid config for [binary_sensor]: "
        "Only one binary sensor of type 'motion' is allowed."
    )
    assert scanner.esphome_import_error(exc) is False


def test_an_unrelated_module_error_is_not_ours():
    import scanner

    assert scanner.esphome_import_error(
        ModuleNotFoundError("No module named 'croniter'")
    ) is False


# ---------------------------------------------------------------------------
# JobQueue.release_for_retry
# ---------------------------------------------------------------------------

@pytest.fixture
def queue(tmp_path):
    return JobQueue(queue_file=tmp_path / "queue.json")


async def _claimed_job(queue):
    job = await queue.enqueue("porch.yaml", "2026.8.1", "run-1", 600)
    job.state = JobState.WORKING
    job.assigned_client_id = "w1"
    job.assigned_hostname = "worker-1"
    return job


def test_release_puts_the_job_back_to_pending(queue):
    async def go():
        job = await _claimed_job(queue)
        assert await queue.release_for_retry(job.id, "installing") is True
        again = queue.get(job.id)
        assert again.state == JobState.PENDING
        # Assignment must be cleared or the next claim looks like a
        # double-assignment to the same worker.
        assert again.assigned_client_id is None
        assert again.assigned_hostname is None
        assert again.assigned_at is None

    asyncio.run(go())


def test_release_is_bounded_by_max_retries(queue):
    """A genuinely stuck install must land on a real failure rather than
    bouncing the job between PENDING and WORKING forever."""
    async def go():
        job = await _claimed_job(queue)
        released = 0
        for _ in range(MAX_RETRIES + 2):
            if await queue.release_for_retry(job.id, "installing"):
                released += 1
                job.state = JobState.WORKING  # simulate the next claim
            else:
                break
        assert released == MAX_RETRIES - 1
        # Caller is told False, so it fails the job with the honest message.
        assert await queue.release_for_retry(job.id, "installing") is False

    asyncio.run(go())


def test_release_refuses_a_job_that_is_not_working(queue):
    """Something else already moved it — don't resurrect it."""
    async def go():
        job = await queue.enqueue("porch.yaml", "2026.8.1", "run-1", 600)
        job.state = JobState.CANCELLED
        assert await queue.release_for_retry(job.id, "installing") is False
        assert queue.get(job.id).state == JobState.CANCELLED

    asyncio.run(go())


def test_release_refuses_an_unknown_job(queue):
    async def go():
        assert await queue.release_for_retry("nope", "installing") is False

    asyncio.run(go())
