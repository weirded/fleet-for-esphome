"""Tests for the typed server↔worker protocol (A.1, A.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from protocol import (
    PROTOCOL_VERSION,
    DeregisterRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    JobAssignment,
    JobLogAppend,
    JobResultSubmission,
    JobStatusUpdate,
    OkResponse,
    ProtocolError,
    RegisterRequest,
    RegisterResponse,
    SystemInfo,
    WorkerLogAppend,
)


# ---------------------------------------------------------------------------
# Round-trip tests — build a model, dump to dict, re-parse, assert equality.
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Every message round-trips JSON → model → dict → model."""

    def test_register_request_roundtrip(self) -> None:
        msg = RegisterRequest(
            hostname="worker-1",
            platform="linux",
            client_version="1.3.1-dev.1",
            image_version="3",
            client_id="abc-123",
            max_parallel_jobs=4,
            system_info=SystemInfo(cpu_cores=8, total_memory="16 GB"),
        )
        d = msg.model_dump()
        msg2 = RegisterRequest.model_validate(d)
        assert msg == msg2
        assert msg2.protocol_version == PROTOCOL_VERSION

    def test_register_response_roundtrip(self) -> None:
        msg = RegisterResponse(client_id="new-id")
        assert RegisterResponse.model_validate(msg.model_dump()) == msg

    def test_heartbeat_request_roundtrip(self) -> None:
        msg = HeartbeatRequest(
            client_id="abc",
            system_info=SystemInfo(cpu_usage=42.5, perf_score=1234),
        )
        assert HeartbeatRequest.model_validate(msg.model_dump()) == msg

    def test_heartbeat_response_roundtrip(self) -> None:
        msg = HeartbeatResponse(
            ok=True,
            server_client_version="1.3.1",
            set_max_parallel_jobs=2,
            clean_build_cache=True,
        )
        assert HeartbeatResponse.model_validate(msg.model_dump()) == msg

    def test_heartbeat_response_image_upgrade_branch(self) -> None:
        msg = HeartbeatResponse(image_upgrade_required=True, min_image_version="3")
        d = msg.model_dump()
        assert d["image_upgrade_required"] is True
        assert d["min_image_version"] == "3"
        assert HeartbeatResponse.model_validate(d) == msg

    def test_deregister_request_roundtrip(self) -> None:
        msg = DeregisterRequest(client_id="abc")
        assert DeregisterRequest.model_validate(msg.model_dump()) == msg

    def test_ok_response_roundtrip(self) -> None:
        assert OkResponse.model_validate({"ok": True}).ok is True
        # Default
        assert OkResponse().ok is True

    def test_job_assignment_roundtrip(self) -> None:
        msg = JobAssignment(
            job_id="j1",
            target="living-room.yaml",
            esphome_version="2026.3.3",
            bundle_b64="AAAA",
            timeout_seconds=900,
            ota_only=False,
            validate_only=False,
            ota_address="10.0.0.5",
            server_timezone="America/Los_Angeles",
        )
        assert JobAssignment.model_validate(msg.model_dump()) == msg

    def test_job_assignment_server_ota_roundtrip(self) -> None:
        msg = JobAssignment(
            job_id="j1",
            target="thread-device.yaml",
            esphome_version="2026.4.3",
            bundle_b64="AAAA",
            server_ota=True,
            ota_address="fd00::1",
        )
        d = msg.model_dump()
        assert d["server_ota"] is True
        assert JobAssignment.model_validate(d) == msg

    def test_job_result_submission_success(self) -> None:
        msg = JobResultSubmission(status="success", ota_result="success")
        assert JobResultSubmission.model_validate(msg.model_dump()) == msg

    def test_job_result_submission_failed(self) -> None:
        msg = JobResultSubmission(status="failed", log="compile error")
        assert JobResultSubmission.model_validate(msg.model_dump()) == msg

    def test_job_status_update_roundtrip(self) -> None:
        msg = JobStatusUpdate(status_text="Compiling…")
        assert JobStatusUpdate.model_validate(msg.model_dump()) == msg

    def test_job_log_append_roundtrip(self) -> None:
        msg = JobLogAppend(lines="INFO[1] foo\nINFO[1] bar\n")
        assert JobLogAppend.model_validate(msg.model_dump()) == msg

    def test_heartbeat_response_stream_logs_roundtrip(self) -> None:
        # WL.2: server tells the worker to start streaming logs.
        msg = HeartbeatResponse(ok=True, stream_logs=True)
        d = msg.model_dump()
        assert d["stream_logs"] is True
        assert HeartbeatResponse.model_validate(d) == msg

    def test_heartbeat_response_stream_logs_stop(self) -> None:
        # Flipping it off again — the worker needs to see the explicit False
        # to tear down its pusher thread.
        msg = HeartbeatResponse(stream_logs=False)
        assert HeartbeatResponse.model_validate(msg.model_dump()).stream_logs is False

    def test_register_request_disk_quota_roundtrip(self) -> None:
        """DQ.6 — RegisterRequest carries the worker's boot-time disk-quota override."""
        msg = RegisterRequest(
            hostname="worker-1",
            platform="linux",
            disk_quota_bytes=5 * 1024 ** 3,
        )
        d = msg.model_dump()
        assert d["disk_quota_bytes"] == 5 * 1024 ** 3
        assert RegisterRequest.model_validate(d) == msg

    def test_register_request_disk_quota_omitted_is_none(self) -> None:
        msg = RegisterRequest(hostname="worker-1", platform="linux")
        assert msg.disk_quota_bytes is None

    def test_heartbeat_response_set_disk_quota_roundtrip(self) -> None:
        """DQ.6 — server pushes the effective disk quota to the worker."""
        msg = HeartbeatResponse(set_disk_quota_bytes=10 * 1024 ** 3)
        d = msg.model_dump()
        assert d["set_disk_quota_bytes"] == 10 * 1024 ** 3
        assert HeartbeatResponse.model_validate(d) == msg

    def test_system_info_disk_quota_fields_roundtrip(self) -> None:
        """DQ.6 — worker reports its current view back via SystemInfo."""
        msg = SystemInfo(
            disk_usage_bytes=2 * 1024 ** 3,
            disk_quota_bytes=10 * 1024 ** 3,
            last_eviction_freed_bytes=512 * 1024 ** 2,
        )
        d = msg.model_dump()
        assert d["disk_usage_bytes"] == 2 * 1024 ** 3
        assert d["disk_quota_bytes"] == 10 * 1024 ** 3
        assert d["last_eviction_freed_bytes"] == 512 * 1024 ** 2
        assert SystemInfo.model_validate(d) == msg

    def test_system_info_disk_quota_fields_default_none(self) -> None:
        msg = SystemInfo()
        assert msg.disk_usage_bytes is None
        assert msg.disk_quota_bytes is None
        assert msg.last_eviction_freed_bytes is None

    def test_heartbeat_response_stream_logs_omitted_is_none(self) -> None:
        # No flag = no change from the worker's current state. Default must
        # be None so we can distinguish "unchanged" from "stop".
        msg = HeartbeatResponse()
        assert msg.stream_logs is None

    def test_worker_log_append_roundtrip(self) -> None:
        # WL.2: the payload the worker sends to POST /api/v1/workers/{id}/logs.
        msg = WorkerLogAppend(offset=0, lines="2026-04-23 INFO foo\n")
        assert WorkerLogAppend.model_validate(msg.model_dump()) == msg

    def test_worker_log_append_with_nonzero_offset(self) -> None:
        # Subsequent pushes carry the byte-offset since worker process start.
        msg = WorkerLogAppend(offset=1234, lines="later line\n")
        d = msg.model_dump()
        assert d["offset"] == 1234
        assert WorkerLogAppend.model_validate(d) == msg

    def test_worker_log_append_empty_lines_allowed(self) -> None:
        # The heartbeat pusher may fire with nothing to send; we'd rather
        # not special-case the empty path — the server can accept and
        # no-op it.
        msg = WorkerLogAppend(offset=0, lines="")
        assert WorkerLogAppend.model_validate(msg.model_dump()) == msg

    def test_protocol_error_roundtrip(self) -> None:
        err = ProtocolError(error="invalid_payload", reason="hostname: field required")
        parsed = ProtocolError.model_validate(err.model_dump())
        assert parsed.error == "invalid_payload"
        assert parsed.reason == "hostname: field required"
        assert parsed.protocol_version == PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Compatibility tests — newer/older peers must not break each other.
# ---------------------------------------------------------------------------


class TestForwardCompatibility:
    """Receiving a payload with new unknown fields must not break an old peer."""

    def test_register_request_ignores_unknown_fields(self) -> None:
        # Older server receiving a newer worker's payload with extra fields.
        msg = RegisterRequest.model_validate({
            "hostname": "w1",
            "platform": "linux",
            "max_parallel_jobs": 1,
            "future_field": "should be ignored",
            "another_new_field": {"nested": 42},
        })
        assert msg.hostname == "w1"
        # Unknown fields are dropped, not stored.
        assert not hasattr(msg, "future_field")

    def test_heartbeat_response_ignores_unknown_fields(self) -> None:
        # Older worker receiving a newer server's response.
        msg = HeartbeatResponse.model_validate({
            "ok": True,
            "server_client_version": "9.9.9",
            "new_feature_flag": True,
        })
        assert msg.ok is True
        assert msg.server_client_version == "9.9.9"

    def test_worker_log_append_ignores_unknown_fields(self) -> None:
        # Older server receiving a newer worker's log push with fields it
        # doesn't know yet (e.g. session_id added in a later release).
        msg = WorkerLogAppend.model_validate({
            "offset": 42,
            "lines": "hello\n",
            "future_session_id": "abc-xyz",
        })
        assert msg.offset == 42
        assert msg.lines == "hello\n"

    def test_job_assignment_ignores_unknown_fields(self) -> None:
        msg = JobAssignment.model_validate({
            "job_id": "j1",
            "target": "t.yaml",
            "esphome_version": "2026.3.3",
            "bundle_b64": "",
            "experimental_parallel_flag": True,
        })
        assert msg.job_id == "j1"

    def test_system_info_ignores_unknown_fields(self) -> None:
        si = SystemInfo.model_validate({
            "cpu_cores": 4,
            "gpu_model": "future field",  # not in model
        })
        assert si.cpu_cores == 4


class TestBackwardCompatibility:
    """Older peers send payloads without fields the newer peer added as optional."""

    def test_register_request_with_minimal_fields(self) -> None:
        # An older worker that doesn't send protocol_version, image_version,
        # client_id, or system_info.
        msg = RegisterRequest.model_validate({
            "hostname": "old-worker",
            "platform": "linux",
        })
        assert msg.hostname == "old-worker"
        assert msg.client_version is None
        assert msg.image_version is None
        assert msg.client_id is None
        assert msg.system_info is None
        # protocol_version defaulted to current
        assert msg.protocol_version == PROTOCOL_VERSION
        assert msg.max_parallel_jobs == 1

    def test_heartbeat_request_with_minimal_fields(self) -> None:
        msg = HeartbeatRequest.model_validate({"client_id": "abc"})
        assert msg.client_id == "abc"
        assert msg.system_info is None

    def test_heartbeat_response_with_only_ok(self) -> None:
        msg = HeartbeatResponse.model_validate({"ok": True})
        assert msg.ok is True
        assert msg.server_client_version is None
        assert msg.image_upgrade_required is None

    def test_job_assignment_server_ota_defaults_false(self) -> None:
        msg = JobAssignment.model_validate({
            "job_id": "j1",
            "target": "t.yaml",
            "esphome_version": "2026.3.0",
            "bundle_b64": "",
        })
        assert msg.server_ota is False


# ---------------------------------------------------------------------------
# Rejection tests — malformed payloads must raise ValidationError.
# ---------------------------------------------------------------------------


class TestRejection:
    def test_register_request_missing_hostname(self) -> None:
        with pytest.raises(ValidationError) as exc:
            RegisterRequest.model_validate({"platform": "linux"})
        assert "hostname" in str(exc.value)

    def test_register_request_missing_platform(self) -> None:
        with pytest.raises(ValidationError):
            RegisterRequest.model_validate({"hostname": "w"})

    def test_heartbeat_request_missing_client_id(self) -> None:
        with pytest.raises(ValidationError):
            HeartbeatRequest.model_validate({})

    def test_job_result_rejects_unknown_status(self) -> None:
        with pytest.raises(ValidationError):
            JobResultSubmission.model_validate({"status": "kaboom"})

    def test_job_result_requires_status(self) -> None:
        with pytest.raises(ValidationError):
            JobResultSubmission.model_validate({})

    def test_job_assignment_missing_required_field(self) -> None:
        with pytest.raises(ValidationError):
            JobAssignment.model_validate({
                "job_id": "j1",
                # missing target, esphome_version, bundle_b64
            })

    def test_register_request_rejects_non_int_max_parallel_jobs(self) -> None:
        with pytest.raises(ValidationError):
            RegisterRequest.model_validate({
                "hostname": "w",
                "platform": "linux",
                "max_parallel_jobs": "lots",
            })


# ---------------------------------------------------------------------------
# Invariants — structural properties that must hold.
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_protocol_version_is_a_positive_int(self) -> None:
        assert isinstance(PROTOCOL_VERSION, int)
        assert PROTOCOL_VERSION >= 1

    def test_server_and_client_protocol_files_are_identical(self) -> None:
        """CI check — the two copies of protocol.py must be byte-identical."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        server_copy = repo_root / "ha-addon" / "server" / "protocol.py"
        client_copy = repo_root / "ha-addon" / "client" / "protocol.py"
        assert server_copy.read_bytes() == client_copy.read_bytes(), (
            "ha-addon/server/protocol.py and ha-addon/client/protocol.py have "
            "diverged. They must be byte-identical — update one, then copy it "
            "to the other."
        )
