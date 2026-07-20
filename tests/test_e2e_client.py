"""End-to-end tests: fake HTTP server + fake esphome binary -> run_job().

The fake server implements the four client-facing endpoints:
  POST /api/v1/clients/register
  POST /api/v1/clients/heartbeat
  GET  /api/v1/jobs/next
  POST /api/v1/jobs/{id}/result

A fake esphome shell script stands in for the real binary so no ESPHome
installation is required.  FakeVersionManager returns its path directly,
bypassing all venv creation.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure SERVER_URL / SERVER_TOKEN are present before client.py is imported
# (they are read at module level).
os.environ.setdefault("SERVER_URL", "http://127.0.0.1:1")
os.environ.setdefault("SERVER_TOKEN", "test-token")

import client as client_mod
from version_manager import VersionManager


# ---------------------------------------------------------------------------
# Bundle builder
# ---------------------------------------------------------------------------

def _make_bundle(*targets: tuple[str, str]) -> str:
    """Return a base64-encoded tar.gz containing one file per (name, content) pair."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in targets:
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def _simple_bundle(target_name: str) -> str:
    return _make_bundle((target_name, "esphome:\n  name: test\n"))


# ---------------------------------------------------------------------------
# Fake HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Minimal handler that serves the client API and records all calls."""

    def log_message(self, fmt, *args):  # suppress default access log noise
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict:
        raw = self._read_body()
        return json.loads(raw) if raw else {}

    def _respond(self, status: int, body: dict | None = None) -> None:
        if status == 204:
            self.send_response(204)
            self.end_headers()
            return
        payload = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        srv: FakeServer = self.server._fake  # type: ignore[attr-defined]

        # Firmware upload: POST /api/v1/jobs/{id}/firmware/{variant}
        # Body is raw bytes (application/octet-stream), not JSON.
        parts = self.path.split("/")
        if (
            len(parts) >= 7
            and parts[1] == "api" and parts[2] == "v1" and parts[3] == "jobs"
            and parts[5] == "firmware"
        ):
            raw = self._read_body()
            variant = parts[6] if len(parts) > 6 else "firmware"
            with srv._seq_lock:
                srv._call_sequence.append(("firmware", variant))
                srv.firmware_calls.append({"variant": variant, "size": len(raw)})
            self._respond(200, {"ok": True})
            return

        body = self._read_json()

        if self.path in ("/api/v1/clients/register", "/api/v1/workers/register"):
            srv.register_calls.append(body)
            self._respond(200, {"client_id": srv.client_id})

        elif self.path in ("/api/v1/clients/heartbeat", "/api/v1/workers/heartbeat"):
            srv.heartbeat_calls.append(body)
            self._respond(200, {"ok": True})

        elif self.path.startswith("/api/v1/jobs/") and self.path.endswith("/result"):
            with srv._seq_lock:
                srv._call_sequence.append(("result", body.get("status", "?")))
                srv.result_calls.append(body)
            self._respond(200, {"ok": True})

        elif self.path.startswith("/api/v1/jobs/") and self.path.endswith("/log"):
            srv.log_lines.append(body.get("lines", ""))
            self._respond(200, {"ok": True})

        elif self.path.startswith("/api/v1/jobs/") and self.path.endswith("/status"):
            self._respond(200, {"ok": True})

        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self) -> None:
        srv: FakeServer = self.server._fake  # type: ignore[attr-defined]

        if self.path.startswith("/api/v1/jobs/next"):
            job = srv.next_job()
            self._respond(204) if job is None else self._respond(200, job)
        else:
            self._respond(404, {"error": "not found"})


class FakeServer:
    """Real HTTP server in a daemon thread; accumulates observed calls."""

    client_id = "fake-client-001"

    def __init__(self) -> None:
        self.register_calls: list[dict] = []
        self.heartbeat_calls: list[dict] = []
        self.result_calls: list[dict] = []
        self.firmware_calls: list[dict] = []
        # Ordered log of ("firmware", variant) / ("result", status) events.
        # Used to assert that all firmware uploads land BEFORE submit_result.
        self._call_sequence: list[tuple[str, str]] = []
        self.log_lines: list[str] = []
        self._jobs: list[dict] = []
        self._lock = threading.Lock()
        self._seq_lock = threading.Lock()

        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd._fake = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def streamed_log(self) -> str:
        """Return all streamed log lines concatenated."""
        return "".join(self.log_lines)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()

    def enqueue_job(self, job: dict) -> None:
        with self._lock:
            self._jobs.append(job)

    def next_job(self) -> dict | None:
        with self._lock:
            return self._jobs.pop(0) if self._jobs else None


@pytest.fixture()
def fake_server():
    srv = FakeServer()
    srv.start()
    yield srv
    srv.stop()


# ---------------------------------------------------------------------------
# Fake VersionManager and esphome binaries
# ---------------------------------------------------------------------------

class FakeVersionManager:
    """Returns a pre-built fake esphome binary without touching the filesystem."""

    def __init__(self, bin_path: str) -> None:
        self._bin = bin_path

    def ensure_version(self, version: str) -> str:  # noqa: ARG002
        return self._bin


@pytest.fixture()
def esphome_ok(tmp_path) -> str:
    """Fake esphome: compile and upload both succeed."""
    p = tmp_path / "esphome"
    p.write_text("#!/bin/sh\necho \"fake esphome $*\"\nexit 0\n")
    p.chmod(0o755)
    return str(p)


@pytest.fixture()
def esphome_compile_fail(tmp_path) -> str:
    """Fake esphome: run/compile exits 1; upload would succeed."""
    p = tmp_path / "esphome"
    p.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "run" ] || [ "$1" = "compile" ]; then echo "ERROR: compile failed"; exit 1; fi\n'
        'echo "fake esphome $*"; exit 0\n'
    )
    p.chmod(0o755)
    return str(p)


@pytest.fixture()
def esphome_ota_fail(tmp_path) -> str:
    """Fake esphome: run fails after compile success (OTA failure); upload also fails."""
    p = tmp_path / "esphome"
    p.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "run" ]; then echo "INFO Successfully compiled program."; echo "ERROR: OTA failed"; exit 1; fi\n'
        'if [ "$1" = "upload" ]; then echo "ERROR: OTA failed"; exit 1; fi\n'
        'echo "fake esphome $*"; exit 0\n'
    )
    p.chmod(0o755)
    return str(p)


@pytest.fixture()
def esphome_writes_firmware(tmp_path) -> str:
    """Fake esphome: run succeeds and writes firmware binaries in ESPHome's
    expected layout under cwd so _collect_firmware_variants finds them.

    ESPHome writes to: .esphome/build/<device>/.pioenvs/<device>/firmware.bin
    (and firmware.factory.bin for ESP32). We use device name "testdevice" and
    produce both variants so the full upload path is exercised.
    """
    p = tmp_path / "esphome"
    p.write_text(
        '#!/bin/sh\n'
        # Print the success marker ESPHome emits
        'echo "INFO Successfully compiled program."\n'
        # Create both firmware variants in the expected tree under cwd
        'mkdir -p ".esphome/build/testdevice/.pioenvs/testdevice"\n'
        'echo "FAKE_OTA_BIN" > ".esphome/build/testdevice/.pioenvs/testdevice/firmware.bin"\n'
        'echo "FAKE_FACTORY_BIN" > ".esphome/build/testdevice/.pioenvs/testdevice/firmware.factory.bin"\n'
        'exit 0\n'
    )
    p.chmod(0o755)
    return str(p)


# ---------------------------------------------------------------------------
# Fixture: redirect _ESPHOME_VERSIONS_DIR to a temp dir so the stable
# build-dir strategy (#13) doesn't write to /esphome-versions/ on the host.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_versions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "_ESPHOME_VERSIONS_DIR", str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patched(srv: FakeServer):
    """Context manager that redirects client HTTP calls to the fake server."""
    return patch.multiple(
        client_mod,
        SERVER_URL=srv.url,
        HEADERS={"Authorization": "Bearer test", "Content-Type": "application/json"},
    )


def _make_job(job_id: str, target: str, bundle_b64: str, version: str = "2024.3.0") -> dict:
    return {
        "job_id": job_id,
        "target": target,
        "esphome_version": version,
        "bundle_b64": bundle_b64,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRegister:
    def test_returns_client_id(self, fake_server):
        with _patched(fake_server):
            client_id = client_mod.register()

        assert client_id == fake_server.client_id

    def test_sends_hostname_and_platform(self, fake_server):
        with _patched(fake_server):
            client_mod.register()

        call = fake_server.register_calls[0]
        assert "hostname" in call
        assert "platform" in call


class TestSuccessfulJob:
    def test_compile_result_posted(self, fake_server, esphome_ok):
        target = "living_room.yaml"
        job = _make_job("job-001", target, _simple_bundle(target))

        with _patched(fake_server):
            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_ok))

        compile_result = fake_server.result_calls[0]
        assert compile_result["status"] == "success"

    def test_ota_result_posted_with_compile(self, fake_server, esphome_ok):
        """esphome run succeeds → single result with status=success and ota_result=success."""
        target = "living_room.yaml"
        job = _make_job("job-001", target, _simple_bundle(target))

        with _patched(fake_server):
            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_ok))

        assert len(fake_server.result_calls) == 1
        result = fake_server.result_calls[0]
        assert result["status"] == "success"
        assert result["ota_result"] == "success"


class TestCompileFailure:
    def test_failure_result_posted(self, fake_server, esphome_compile_fail):
        target = "bedroom.yaml"
        job = _make_job("job-002", target, _simple_bundle(target))

        with _patched(fake_server):
            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_compile_fail))

        assert fake_server.result_calls[0]["status"] == "failed"

    def test_ota_not_attempted_after_compile_failure(self, fake_server, esphome_compile_fail):
        target = "bedroom.yaml"
        job = _make_job("job-002", target, _simple_bundle(target))

        with _patched(fake_server):
            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_compile_fail))

        # Only one result POST; no OTA call should follow a compile failure
        assert len(fake_server.result_calls) == 1

    def test_compile_output_included_in_log(self, fake_server, esphome_compile_fail):
        target = "bedroom.yaml"
        job = _make_job("job-002", target, _simple_bundle(target))

        with _patched(fake_server):
            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_compile_fail))

        # Log is streamed via /log endpoint, not in the result body
        assert "compile failed" in fake_server.streamed_log.lower()


class TestOtaFailure:
    def test_ota_failure_reported(self, fake_server, esphome_ota_fail):
        """esphome run fails after compile success → retries upload → reports ota_result=failed."""
        target = "sensor.yaml"
        job = _make_job("job-003", target, _simple_bundle(target))

        with _patched(fake_server):
            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_ota_fail))

        result = fake_server.result_calls[0]
        assert result["status"] == "success"
        assert result["ota_result"] == "failed"


class TestBundleEdgeCases:
    def test_missing_target_reports_failure(self, fake_server, esphome_ok):
        # Bundle has 'other.yaml'; job targets 'missing.yaml'
        bundle_b64 = _make_bundle(("other.yaml", "esphome:\n  name: other\n"))
        job = _make_job("job-004", "missing.yaml", bundle_b64)

        with _patched(fake_server):
            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_ok))

        result = fake_server.result_calls[0]
        assert result["status"] == "failed"
        assert "not found in bundle" in result["log"]

    def test_bundle_with_multiple_files(self, fake_server, esphome_ok):
        target = "switch.yaml"
        bundle_b64 = _make_bundle(
            ("secrets.yaml", "wifi_password: secret"),
            (target, "esphome:\n  name: switch\n"),
            ("packages/common.yaml", "wifi: !include common.yaml\n"),
        )
        job = _make_job("job-005", target, bundle_b64)

        with _patched(fake_server):
            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_ok))

        assert fake_server.result_calls[0]["status"] == "success"


class TestPollCycle:
    def test_job_fetched_via_http_then_executed(self, fake_server, esphome_ok):
        """Simulate the poll loop: GET /jobs/next from server, then run the job."""
        target = "kitchen.yaml"
        fake_server.enqueue_job(_make_job("job-poll-001", target, _simple_bundle(target)))

        with _patched(fake_server):
            resp = client_mod.get("/api/v1/jobs/next")
            assert resp.status_code == 200
            job = resp.json()
            assert job["job_id"] == "job-poll-001"

            client_mod.run_job(fake_server.client_id, job, FakeVersionManager(esphome_ok))

        assert len(fake_server.result_calls) == 1
        assert fake_server.result_calls[0]["status"] == "success"
        assert fake_server.result_calls[0]["ota_result"] == "success"

    def test_no_job_returns_204(self, fake_server):
        """Server returns 204 when the queue is empty."""
        with _patched(fake_server):
            resp = client_mod.get("/api/v1/jobs/next")

        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Bug #236 regression: firmware-variant uploads must arrive at the server
# BEFORE submit_result(success) so the server's "job must be WORKING" gate
# on the firmware-upload endpoint never fires with 409.
# ---------------------------------------------------------------------------

class TestFirmwareUploadOrdering:
    """Regression for #236: firmware uploads race the job-success transition.

    The server's /api/v1/jobs/{id}/firmware/{variant} endpoint enforces
    job.state == WORKING; once submit_result transitions the job to SUCCESS
    the endpoint returns 409 job_not_working.  Invariant: every firmware
    upload must be recorded BEFORE the result submission in FakeServer's
    call-sequence log.
    """

    def test_ota_success_uploads_firmware_before_submit_result(
        self, fake_server, esphome_writes_firmware
    ):
        """OTA-success path: both firmware variants upload before submit_result."""
        target = "living_room.yaml"
        job = _make_job("job-236", target, _simple_bundle(target))

        with _patched(fake_server):
            client_mod.run_job(
                fake_server.client_id, job,
                FakeVersionManager(esphome_writes_firmware),
            )

        seq = fake_server._call_sequence
        # At least one firmware upload must have been recorded.
        firmware_events = [e for e in seq if e[0] == "firmware"]
        result_events = [e for e in seq if e[0] == "result"]

        assert firmware_events, "no firmware variant was uploaded to the server"
        assert result_events, "no result was submitted"

        # Every firmware upload must precede the first result submission.
        first_result_idx = next(i for i, e in enumerate(seq) if e[0] == "result")
        last_firmware_idx = max(i for i, e in enumerate(seq) if e[0] == "firmware")
        assert last_firmware_idx < first_result_idx, (
            f"firmware upload at seq[{last_firmware_idx}] came AFTER "
            f"submit_result at seq[{first_result_idx}]; "
            f"full sequence: {seq}"
        )

        # Both variants (ota + factory) should have been uploaded.
        uploaded_variants = {e[1] for e in firmware_events}
        assert "ota" in uploaded_variants, f"missing 'ota' variant; got {uploaded_variants}"
        assert "factory" in uploaded_variants, f"missing 'factory' variant; got {uploaded_variants}"

        # Job must still be reported as success.
        assert result_events[0][1] == "success", f"expected success, got {result_events[0][1]}"

    def test_download_only_uploads_firmware_before_submit_result(
        self, fake_server, esphome_writes_firmware
    ):
        """Download-only path: firmware upload precedes submit_result."""
        target = "sensor.yaml"
        job = {**_make_job("job-236b", target, _simple_bundle(target)), "download_only": True}

        with _patched(fake_server):
            client_mod.run_job(
                fake_server.client_id, job,
                FakeVersionManager(esphome_writes_firmware),
            )

        seq = fake_server._call_sequence
        firmware_events = [e for e in seq if e[0] == "firmware"]
        result_events = [e for e in seq if e[0] == "result"]

        assert firmware_events, "no firmware uploaded for download_only job"
        assert result_events, "no result submitted for download_only job"

        first_result_idx = next(i for i, e in enumerate(seq) if e[0] == "result")
        last_firmware_idx = max(i for i, e in enumerate(seq) if e[0] == "firmware")
        assert last_firmware_idx < first_result_idx, (
            f"firmware upload at seq[{last_firmware_idx}] came AFTER "
            f"submit_result at seq[{first_result_idx}]; full sequence: {seq}"
        )
        assert result_events[0][1] == "success"


# ---------------------------------------------------------------------------
# Integration tests — real ESPHome compile via VersionManager
#
# Run locally:
#   pytest -m integration -v -s
#
# Run in Docker (matches production environment, caches ESPHome venvs):
#   docker build -f tests/Dockerfile.integration -t esphome-dist-test .
#   docker run --rm -v esphome-versions-cache:/esphome-versions esphome-dist-test
# ---------------------------------------------------------------------------

# Override with ESPHOME_TEST_VERSION env var if you want a different version.
ESPHOME_TEST_VERSION = os.environ.get("ESPHOME_TEST_VERSION", "2026.7.0")

_COMPILE_TEST_YAML = """\
esphome:
  name: compile-test

esp8266:
  board: d1_mini

wifi:
  ssid: "test-network"
  password: "test-password"

logger:
"""


@pytest.fixture(scope="session")
def real_version_manager(tmp_path_factory):
    """VersionManager that installs ESPHome for real.

    Uses ESPHOME_VERSIONS_DIR if set (Docker volume), otherwise a session-scoped
    temp dir.  Session-scoped so ESPHome is only installed once per pytest run.
    """
    base_dir = os.environ.get("ESPHOME_VERSIONS_DIR")
    if base_dir:
        base = Path(base_dir)
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = tmp_path_factory.mktemp("esphome-versions")
    return VersionManager(versions_base=base, max_versions=2)


@pytest.mark.integration
class TestRealEspHomeCompile:
    """Runs an actual ESPHome compile against the fake server.

    VersionManager installs the requested ESPHome version into a venv — no
    pre-installed esphome required.  The fake HTTP server records all calls so
    results can be asserted without a real server running.
    """

    def test_compile_succeeds(self, fake_server, real_version_manager):
        """ESPHome compiles the minimal fixture config without errors."""
        target = "compile-test.yaml"
        bundle_b64 = _make_bundle((target, _COMPILE_TEST_YAML))
        job = _make_job("real-job-001", target, bundle_b64, version=ESPHOME_TEST_VERSION)

        with _patched(fake_server), patch.object(client_mod, "OTA_TIMEOUT", 10):
            client_mod.run_job(fake_server.client_id, job, real_version_manager)

        compile_call = fake_server.result_calls[0]
        assert compile_call["status"] == "success", (
            f"ESPHome compile failed. Log:\n{compile_call.get('log', '(no log)')}"
        )

    def test_ota_result_is_reported_after_compile(self, fake_server, real_version_manager):
        """OTA result (pass or fail — no device present) is always reported."""
        target = "compile-test.yaml"
        bundle_b64 = _make_bundle((target, _COMPILE_TEST_YAML))
        job = _make_job("real-job-002", target, bundle_b64, version=ESPHOME_TEST_VERSION)

        with _patched(fake_server), patch.object(client_mod, "OTA_TIMEOUT", 10):
            client_mod.run_job(fake_server.client_id, job, real_version_manager)

        assert len(fake_server.result_calls) >= 1, (
            "Expected at least one result call; got: "
            + str([c for c in fake_server.result_calls])
        )
        # With esphome run, result includes ota_result in the same call
        assert "ota_result" in fake_server.result_calls[0]

    def test_compile_log_is_non_empty(self, fake_server, real_version_manager):
        """The compile log captured from esphome stdout is returned to the server."""
        target = "compile-test.yaml"
        bundle_b64 = _make_bundle((target, _COMPILE_TEST_YAML))
        job = _make_job("real-job-003", target, bundle_b64, version=ESPHOME_TEST_VERSION)

        with _patched(fake_server), patch.object(client_mod, "OTA_TIMEOUT", 10):
            client_mod.run_job(fake_server.client_id, job, real_version_manager)

        # Log is streamed via /log endpoint, not in the result body
        assert len(fake_server.streamed_log) > 0, "Expected non-empty streamed compile log"
