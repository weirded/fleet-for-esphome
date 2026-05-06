"""ESPHome distributed build worker — polling loop, heartbeat, job runner."""

from __future__ import annotations

import base64
import fcntl
import io
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import requests
from pydantic import ValidationError


from protocol import (
    DeregisterRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    JobAssignment,
    JobLogAppend,
    JobResultSubmission,
    JobStatusUpdate,
    RegisterRequest,
    RegisterResponse,
    SystemInfo,
    WorkerDiagnosticsUpload,
    WorkerLogAppend,
)
from version_manager import VersionManager
from sysinfo import collect_system_info

# ---------------------------------------------------------------------------
# Client version — must match the add-on VERSION file; bumped on each release.
# The server returns this value in heartbeat responses so outdated clients
# can detect the mismatch and self-update.
# ---------------------------------------------------------------------------

CLIENT_VERSION = "1.7.1-dev.6"


def _read_image_version() -> Optional[str]:
    """Read the baked-in Docker image version from IMAGE_VERSION next to this file.

    Returns None if the file is missing (e.g. running from a source checkout
    without a Docker build). The server treats None as "unknown".
    """
    try:
        path = Path(__file__).parent / "IMAGE_VERSION"
        return path.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, OSError):
        return None


IMAGE_VERSION = _read_image_version()


# ---------------------------------------------------------------------------
# Logging setup — per-worker context filter
# Injects "[w<N> <target>] " prefix so each line shows which worker slot and
# which YAML file produced it, making parallel build logs easy to follow.
# ---------------------------------------------------------------------------

_log_context = threading.local()


class _WorkerContextFilter(logging.Filter):
    """Inject worker context prefix into every log record from this thread."""

    def filter(self, record: logging.LogRecord) -> bool:
        worker_id = getattr(_log_context, "worker_id", None)
        target = getattr(_log_context, "current_target", None)
        if worker_id is not None:
            if target:
                short = os.path.basename(target).rsplit(".", 1)[0]
                record.ctx = f"[w{worker_id} {short}] "  # type: ignore[attr-defined]
            else:
                record.ctx = f"[w{worker_id}] "  # type: ignore[attr-defined]
        else:
            record.ctx = ""  # type: ignore[attr-defined]
        return True


logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s %(levelname)-8s v{CLIENT_VERSION} %(ctx)s%(name)s: %(message)s",
)
# Attach the filter to the root handler so it runs for every log record.
for _h in logging.getLogger().handlers:
    _h.addFilter(_WorkerContextFilter())

# WL.1: capture every formatted record into a bounded ring buffer so
# the pull-when-watched pusher (WL.2) has a backlog ready whenever the
# server's ``stream_logs`` flag flips on. Handler is a tee — the
# existing StreamHandler to stdout is unchanged.
from log_capture import LogCaptureHandler  # noqa: E402

_log_capture = LogCaptureHandler()
_log_capture.setFormatter(
    logging.Formatter(f"%(asctime)s %(levelname)-8s v{CLIENT_VERSION} %(ctx)s%(name)s: %(message)s")
)
_log_capture.addFilter(_WorkerContextFilter())
logging.getLogger().addHandler(_log_capture)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

SERVER_URL = os.environ["SERVER_URL"].rstrip("/")
SERVER_TOKEN = os.environ["SERVER_TOKEN"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "10"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "600"))
OTA_TIMEOUT = int(os.environ.get("OTA_TIMEOUT", "120"))
MAX_ESPHOME_VERSIONS = int(os.environ.get("MAX_ESPHOME_VERSIONS", "3"))
MAX_PARALLEL_JOBS = int(os.environ.get("MAX_PARALLEL_JOBS", "2"))
HOSTNAME = os.environ.get("HOSTNAME", socket.gethostname())
PLATFORM = os.environ.get("PLATFORM", sys.platform)
# TG.1: comma-separated tag list. Empty / unset → register without seeding.
# WORKER_TAGS_OVERWRITE=1 forces clobber-on-registration so scripted multi-
# worker deployments retain the "tags travel with the docker invocation"
# semantics (server-side wins is the default).
def _parse_tags_env(raw: Optional[str]) -> Optional[list[str]]:
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]
WORKER_TAGS = _parse_tags_env(os.environ.get("WORKER_TAGS"))
WORKER_TAGS_OVERWRITE = os.environ.get("WORKER_TAGS_OVERWRITE", "0").strip().lower() in ("1", "true", "yes")
# DQ.9: per-worker disk-quota override baked into the docker invocation.
# Integer GiB; converted to bytes on the wire. The server seeds its
# persistent override from this on the *first* registration; subsequent
# heartbeats push the effective value (override ?? fleet default) back
# to us via HeartbeatResponse.set_disk_quota_bytes — see _current_disk_quota.
def _parse_disk_quota_gb_env(raw: Optional[str]) -> Optional[int]:
    if raw is None or not raw.strip():
        return None
    try:
        gb = int(raw.strip())
    except ValueError:
        logger.warning("Ignoring invalid WORKER_DISK_QUOTA_GB=%r (not an integer)", raw)
        return None
    if gb < 1:
        logger.warning("Ignoring invalid WORKER_DISK_QUOTA_GB=%d (must be ≥ 1)", gb)
        return None
    return gb * 1024 ** 3
WORKER_DISK_QUOTA_BYTES = _parse_disk_quota_gb_env(os.environ.get("WORKER_DISK_QUOTA_GB"))
# DQ.9: emergency host-disk floor — same default as version_manager (10%);
# triggers an out-of-quota sweep in disk_quota.host_disk_floor when host
# free% drops below this regardless of our usage. Workers share the disk
# with HA core, Docker, and logs.
MIN_FREE_DISK_PCT = int(os.environ.get("MIN_FREE_DISK_PCT", "10"))
ESPHOME_BIN = os.environ.get("ESPHOME_BIN")  # If set, skip version manager
ESPHOME_SEED_VERSION = os.environ.get("ESPHOME_SEED_VERSION")  # Pre-download on startup
# Base directory for per-slot PlatformIO core dirs (avoids cross-slot conflicts)
_ESPHOME_VERSIONS_DIR = os.environ.get("ESPHOME_VERSIONS_DIR", "/esphome-versions")
# Persistent client identity file — survives container restarts
_CLIENT_ID_FILE = os.path.join(_ESPHOME_VERSIONS_DIR, ".client_id")

HEADERS = {
    "Authorization": f"Bearer {SERVER_TOKEN}",
    "Content-Type": "application/json",
}

# Set when the heartbeat detects a newer server-side client bundle.
# Checked in the main loop so updates only happen between jobs.
_update_available: threading.Event = threading.Event()

# Sticky flag so we only log the "image upgrade required" warning once
# per process rather than on every heartbeat.
_image_upgrade_logged: bool = False

# Active job counter — incremented/decremented by run_job(); main loop
# waits for this to reach zero before applying updates or re-registering.
_active_jobs: int = 0
_active_jobs_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Connectivity / auth state — deduplicate repeated log messages
# ---------------------------------------------------------------------------
# Touched by both the heartbeat thread and the worker poll loops. The GIL
# makes individual bool reads atomic, but the test-then-set pattern in the
# helpers below is a race: two threads can both pass the ``if`` check before
# either flips the flag, causing duplicate "went offline" log lines. C.1 wraps
# the test-then-set in a single shared lock.
_state_lock: threading.Lock = threading.Lock()
_server_reachable: bool = True   # False once we've logged "server offline"
_auth_ok: bool = True            # False once we've logged "auth failed"
_reregister_needed: threading.Event = threading.Event()  # set by heartbeat on 404
# Bug #4: clean-cache request received from the server but deferred until
# all active jobs drain. The poll loops also check this and skip claiming
# new jobs while it's set, so the cache only gets wiped on a quiescent
# worker — never mid-compile.
_clean_pending: threading.Event = threading.Event()

# DQ.9: thread-safe disk-quota cell. Boot-time default = WORKER_DISK_QUOTA_GB
# env var (or None — meaning "no opinion, use whatever the server sends").
# The heartbeat handler stores HeartbeatResponse.set_disk_quota_bytes here
# on every response so a UI edit (or a fleet-default change) propagates
# within one heartbeat tick without a worker restart. ``last_eviction_freed_bytes``
# is the bytes freed by the most recent post-job sweep — surfaced in
# system_info so the UI can show "evicted N bytes" toasts.
_disk_quota_lock: threading.Lock = threading.Lock()
_current_disk_quota_bytes: Optional[int] = WORKER_DISK_QUOTA_BYTES
_last_eviction_freed_bytes: int = 0
# DQ.9: active-set tracker for pinning during eviction sweeps. run_job
# uses ``active_job_set.pin(...)`` while a compile is in flight; the
# disk_quota engine reads ``snapshot()`` to know which dirs are off-limits.
from disk_quota import ActiveJobSet  # noqa: E402, PLC0415
_active_job_set: ActiveJobSet = ActiveJobSet()


def _get_current_disk_quota_bytes() -> Optional[int]:
    with _disk_quota_lock:
        return _current_disk_quota_bytes


def _set_current_disk_quota_bytes(value: Optional[int]) -> None:
    global _current_disk_quota_bytes
    with _disk_quota_lock:
        _current_disk_quota_bytes = value


def _record_eviction_freed_bytes(freed: int) -> None:
    global _last_eviction_freed_bytes
    with _disk_quota_lock:
        _last_eviction_freed_bytes = freed


def _get_last_eviction_freed_bytes() -> int:
    with _disk_quota_lock:
        return _last_eviction_freed_bytes


def _build_system_info() -> "SystemInfo":  # noqa: F821
    """Collect sysinfo + enrich with the disk-quota engine's current view.

    Kept as a helper so register and heartbeat both surface the same
    ``disk_usage_bytes`` / ``disk_quota_bytes`` / ``last_eviction_freed_bytes``
    triple (DQ.6). ``compute_usage`` is a single os.scandir walk per call —
    not free, but heartbeats are 10s apart and the dir's small.
    """
    import disk_quota  # noqa: PLC0415

    sysinfo_dict = collect_system_info(_ESPHOME_VERSIONS_DIR)
    base = Path(_ESPHOME_VERSIONS_DIR)
    if base.exists():
        try:
            sysinfo_dict["disk_usage_bytes"] = disk_quota.compute_usage(base).total_bytes
        except Exception:
            logger.debug("disk_quota.compute_usage failed", exc_info=True)
    quota = _get_current_disk_quota_bytes()
    if quota is not None:
        sysinfo_dict["disk_quota_bytes"] = quota
    sysinfo_dict["last_eviction_freed_bytes"] = _get_last_eviction_freed_bytes()
    return SystemInfo.model_validate(sysinfo_dict)


def _is_idle() -> bool:
    """Return True when no jobs are currently running across all workers."""
    with _active_jobs_lock:
        return _active_jobs == 0


def _on_server_unreachable(exc: Exception) -> None:
    global _server_reachable
    with _state_lock:
        if not _server_reachable:
            return
        _server_reachable = False
    logger.warning("Server went offline: %s", exc)


def _on_server_reachable() -> None:
    global _server_reachable
    with _state_lock:
        if _server_reachable:
            return
        _server_reachable = True
    logger.info("Server came back online")


def _on_auth_failed() -> None:
    global _auth_ok
    with _state_lock:
        if not _auth_ok:
            return
        _auth_ok = False
    logger.warning("Authentication failed (token mismatch?) — will keep retrying silently")


def _on_auth_ok() -> None:
    global _auth_ok
    with _state_lock:
        if _auth_ok:
            return
        _auth_ok = True
    logger.info("Authentication restored")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def post(path: str, data: dict, timeout: int = 30) -> requests.Response:
    url = f"{SERVER_URL}{path}"
    return requests.post(url, json=data, headers=HEADERS, timeout=timeout)


def get(path: str, params: Optional[dict] = None, timeout: int = 30) -> requests.Response:
    url = f"{SERVER_URL}{path}"
    return requests.get(url, params=params, headers={**HEADERS, "Content-Type": "application/json"}, timeout=timeout)


def post_bytes(
    path: str, data: bytes, timeout: int = 600, client_id: Optional[str] = None,
) -> requests.Response:
    """POST raw bytes (e.g. firmware uploads — FD.5). 10 min default timeout.

    *client_id* is included as `X-Client-Id` so the server can validate
    that the caller is the worker currently assigned to the job (bug
    #24 / audit F-08). Omit only for test scaffolding.
    """
    url = f"{SERVER_URL}{path}"
    headers = {**HEADERS, "Content-Type": "application/octet-stream"}
    if client_id:
        headers["X-Client-Id"] = client_id
    return requests.post(url, data=data, headers=headers, timeout=timeout)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _load_client_id() -> Optional[str]:
    """Load persisted client_id from disk (survives container restarts)."""
    # Environment override (set by auto-update before os.execv)
    env_id = os.environ.pop("DISTRIBUTED_ESPHOME_CLIENT_ID", None)
    if env_id:
        return env_id
    try:
        if os.path.exists(_CLIENT_ID_FILE):
            with open(_CLIENT_ID_FILE, encoding="utf-8") as f:
                cid = f.read().strip()
                if cid:
                    return cid
    except OSError:
        logger.debug("Could not read client_id file: %s", _CLIENT_ID_FILE, exc_info=True)
    return None


def _save_client_id(client_id: str) -> None:
    """Persist client_id to disk."""
    try:
        os.makedirs(os.path.dirname(_CLIENT_ID_FILE), exist_ok=True)
        with open(_CLIENT_ID_FILE, "w", encoding="utf-8") as f:
            f.write(client_id)
    except OSError as exc:
        logger.debug("Could not persist client_id: %s", exc)


def _clear_client_id() -> None:
    """Remove persisted client_id (on clean deregister)."""
    try:
        if os.path.exists(_CLIENT_ID_FILE):
            os.remove(_CLIENT_ID_FILE)
    except OSError:
        logger.debug("Could not remove client_id file: %s", _CLIENT_ID_FILE, exc_info=True)


def deregister(client_id: str) -> None:
    """Tell the server to remove this worker (best-effort on shutdown)."""
    try:
        resp = post(
            "/api/v1/workers/deregister",
            DeregisterRequest(client_id=client_id).model_dump(),
        )
        if resp.ok:
            logger.info("Deregistered worker %s", client_id)
            _clear_client_id()
        else:
            logger.debug("Deregister returned %s", resp.status_code)
    except Exception as exc:
        logger.debug("Deregister failed: %s", exc)


def register() -> str:
    """Register with server and return client_id. Retries until successful.

    Re-uses a persisted client_id so the server recognises us across restarts.
    """
    existing_id = _load_client_id()
    while True:
        try:
            sysinfo = collect_system_info(_ESPHOME_VERSIONS_DIR)
            req = RegisterRequest(
                hostname=HOSTNAME,
                platform=PLATFORM,
                client_version=CLIENT_VERSION,
                image_version=IMAGE_VERSION,
                client_id=existing_id,
                max_parallel_jobs=MAX_PARALLEL_JOBS,
                system_info=_build_system_info(),
                tags=WORKER_TAGS,
                overwrite_tags=WORKER_TAGS_OVERWRITE,
                disk_quota_bytes=WORKER_DISK_QUOTA_BYTES,
            )
            resp = post("/api/v1/workers/register", req.model_dump(exclude_none=True))
            resp.raise_for_status()
            try:
                parsed = RegisterResponse.model_validate(resp.json())
            except ValidationError as exc:
                raise RuntimeError(f"malformed register response: {exc}") from exc
            client_id = parsed.client_id
            _save_client_id(client_id)
            logger.info("Registered as worker %s (version %s)", client_id, CLIENT_VERSION)
            logger.info(
                "System: %s | %s | %s cores | %s | %s",
                sysinfo.get("os_version", "?"),
                sysinfo.get("cpu_model", "?"),
                sysinfo.get("cpu_cores", "?"),
                sysinfo.get("total_memory", "?"),
                sysinfo.get("cpu_arch", "?"),
            )
            return client_id
        except Exception as exc:
            logger.warning("Registration failed: %s; retrying in 5s", exc)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Heartbeat thread
# ---------------------------------------------------------------------------

def _restart_self() -> None:
    """Restart the worker process in-place (preserving env vars)."""
    logger.info("Restarting worker process...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


# #214 / #220: PlatformIO does not validate package integrity between
# runs — once a `pio-slot-N/` subtree is partially extracted, missing a
# binary, or has lost a file, every subsequent compile on the same slot
# fails identically. The remedy is always the same (wipe the broken
# subtree, retry once, let PIO re-fetch). These regexes match the
# distinct symptoms observed in the home lab so the worker can detect
# every flavor of corruption with one self-heal hook. New patterns get
# added here, not at every call site.
_BROKEN_PIO_SIGNATURES: tuple[re.Pattern[str], ...] = (
    # toolchain-xtensa-esp-elf missing the cc1 binary (#214 original).
    re.compile(r"posix_spawnp.*(?:cc1|C compiler)|(?:cc1|C compiler).*posix_spawnp", re.DOTALL),
    # framework-libs / framework-arduinoespressif32-libs partially extracted —
    # the metadata files are present but the actual library blobs aren't.
    re.compile(r"Missing framework-\S+ package"),
    # tool-* package extracted with no Python project files (only
    # metadata) — PIO's `pip install -e <tool>` then fails with this.
    re.compile(r"does not appear to be a Python project, as neither"),
    # esptool half-installed inside penv/.espidf-*/ — directory exists
    # but missing __init__.py, so Python sees it as a non-package.
    re.compile(r"ModuleNotFoundError: No module named ['\"]esptool|esptool['\"] is not a package"),
    # PIO penv binary missing — sh's "not found" form (followed by the
    # generic Error 127 below).
    re.compile(r"/penv/bin/\S+: not found"),
    # Generic Error 127 from a PIO bootloader/firmware build (catches
    # everything else where some PIO-invoked subcommand is missing).
    re.compile(r"\*\*\* \[\.pioenvs/[^\]]+/(?:bootloader|firmware)\.bin\] Error 127"),
)


def _is_broken_pio_state(log: str) -> bool:
    """#214 / #220: detect signatures of a corrupted ``pio-slot-N/`` tree
    in a PlatformIO compile log. See ``_BROKEN_PIO_SIGNATURES`` for the
    exhaustive list.

    Triggers ``_wipe_broken_toolchain`` + a one-shot retry on the same
    job — the worker re-extracts everything PIO needs from scratch, the
    operator never has to babysit it.
    """
    return any(pat.search(log) for pat in _BROKEN_PIO_SIGNATURES)


def _wipe_broken_toolchain(pio_dir: str) -> bool:
    """#214 / #220: wipe the corrupted parts of ``pio-slot-N/`` so PIO
    can re-bootstrap on the next compile.

    Removes both ``packages/`` (extracted toolchain + framework + tool
    packages) and ``penv/`` (the PIO Python venv where ``esptool``,
    ``esp-coredump``, etc. live). Either subtree can rot independently:
    the original #214 incident was ``packages/toolchain-xtensa-esp-elf``
    missing ``cc1``; #220 added cases where ``penv/bin/esptool`` was
    gone (``Error 127``) and where ``packages/framework-arduinoespressif32-libs``
    held only metadata files. A surgical "wipe just packages/" misses
    the latter two — wipe both.

    The PIO core dir itself, the per-target build slots
    (``slots/<N>/<stem>/``), the shared ESPHome cache, ``dist/``
    (downloaded source archives — re-extracts from these without
    re-download), and ``platforms/`` are all left alone.

    Returns True if at least one subtree was wiped, False if neither
    existed (nothing to recover) or if the disk-pressure guard fired.
    """
    targets = [
        os.path.join(pio_dir, "packages"),
        os.path.join(pio_dir, "penv"),
    ]
    existing = [t for t in targets if os.path.isdir(t)]
    if not existing:
        logger.warning("[#214] no packages/ or penv/ under %s — nothing to wipe", pio_dir)
        return False
    # #219: if the host is out of disk, wiping the broken state only to
    # have the next extract hit ENOSPC mid-tarball (re-corrupting what
    # we just cleaned) is a guaranteed loop. Fail fast with one honest
    # log line the operator can act on. A fresh xtensa toolchain
    # extracts to ~600 MB; require at least 1 GiB free as headroom.
    try:
        usage = shutil.disk_usage(pio_dir)
        free_gib = usage.free / (1024 ** 3)
        if free_gib < 1.0:
            logger.warning(
                "[#214/#219] skipping self-heal of %s — only %.2f GiB free on the "
                "worker's filesystem; a fresh toolchain extract needs >1 GiB. "
                "Free disk on the worker host (Clean cache, prune Docker images, "
                "or grow the volume) and retry.",
                pio_dir, free_gib,
            )
            return False
    except OSError as exc:
        # disk_usage failure is itself diagnostic — log and proceed.
        logger.warning("[#214/#219] disk_usage probe failed on %s: %s", pio_dir, exc)
    wiped_any = False
    for target in existing:
        logger.warning(
            "[#214] wiping broken PlatformIO state at %s — next compile will "
            "re-fetch (~5–10 min cold).",
            target,
        )
        # ef-2n2: two-pass wipe. The strict first pass surfaces real
        # problems (read-only fs, EPERM) in the log. If it raises a
        # benign mid-walk ENOENT — the live AI-MacBook-Pro repro on
        # 2026-05-02 was ``FileNotFoundError: 'CMakeLists.txt'``, a
        # concurrent mutator or already-removed inode — the lenient
        # second pass sweeps whatever's left so the retry compile
        # doesn't land on a half-rotted toolchain. ``os.path.exists``
        # decides ``wiped_any``: a real permission failure leaves the
        # subtree intact through both passes and returns False (the
        # ``test_wipe_broken_toolchain_swallows_rmtree_failure`` case).
        try:
            shutil.rmtree(target, ignore_errors=False)
        except Exception as exc:
            logger.warning(
                "[#214] strict wipe of %s raised %s — sweeping leftover "
                "state with ignore_errors=True.",
                target, exc,
            )
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception:
                pass
        if os.path.exists(target):
            logger.warning(
                "[#214] subtree at %s still present after wipe — retry "
                "compile will likely fail; investigate filesystem state.",
                target,
            )
        else:
            wiped_any = True
    return wiped_any


def _log_toolchain_state(pio_dir: str, reason: str) -> None:
    """#214: dump the PlatformIO toolchain layout to the worker log so the
    next ``cc1: posix_spawnp: No such file or directory`` failure has
    actionable context. Recursive ``ls`` of the xtensa-esp-elf packages
    dir, plus disk-free + active-job count. Best-effort — we never raise.

    Called from the compile failure path when the captured log mentions
    a posix_spawnp ENOENT on cc1, so this only fires when we actually
    need the data (no overhead on the happy path).
    """
    try:
        toolchain_root = os.path.join(pio_dir, "packages")
        if not os.path.isdir(toolchain_root):
            logger.warning("[#214] no packages/ under %s — pio dir was wiped", pio_dir)
            return
        for entry in os.listdir(toolchain_root):
            if "toolchain" not in entry and "esp" not in entry:
                continue
            sub = os.path.join(toolchain_root, entry)
            try:
                proc = subprocess.run(
                    ["/bin/ls", "-laR", sub],
                    check=False, capture_output=True, text=True, timeout=10,
                )
                logger.warning("[#214] %s — %s tree:\n%s", reason, sub, (proc.stdout or "")[:8000])
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("[#214] ls failed on %s: %s", sub, exc)
        try:
            st = os.statvfs(pio_dir)
            free_gb = (st.f_frsize * st.f_bavail) / (1024 ** 3)
            logger.warning("[#214] free disk on %s: %.1f GB", pio_dir, free_gb)
        except OSError:
            pass
        with _active_jobs_lock:
            logger.warning("[#214] active jobs on this worker: %d", _active_jobs)
    except Exception:
        logger.warning("[#214] toolchain-state diagnostic crashed", exc_info=True)


def _clean_build_cache() -> None:
    """Remove build artifacts from the esphome-versions directory.

    Preserves both:
      - **Installed ESPHome venvs** (anything with ``bin/esphome``) —
        bug #119; the embedded local-worker shares
        ``/data/esphome-versions/`` with the server's lazy-installed
        venv cache (see ``main.py``'s
        ``ESPHOME_VERSIONS_DIR=/data/esphome-versions`` in the local-
        worker spawn). Pre-#119, Clean Cache wiped the server's active
        venv, leaving ``scanner._server_esphome_bin`` pointing at a
        deleted path and every subsequent bundle failing with
        ``FileNotFoundError: '.../bin/python'`` until the add-on
        restarted. Venv lifecycle is already bounded by
        ``MAX_ESPHOME_VERSIONS`` LRU eviction in ``VersionManager``,
        so leaving them in place is not a leak.
      - **PlatformIO core dirs** (``pio-slot-*/`` — toolchain home) —
        bug #214; the xtensa-esp-elf toolchain is ~500 MB and takes
        5–10 min to re-download via curl/tar. Wiping it on every Clean
        Cache click forces every subsequent compile through that
        re-download, and a partially-extracted toolchain surfaces as
        ``cc1: posix_spawnp: No such file or directory`` on jobs that
        race the first post-clean compile. The user's intent for Clean
        Cache is "wipe build artifacts so the next compile is honest"
        (i.e. ``slots/<N>/<stem>/`` and ``cache/<stem>/``), NOT "spend
        10 minutes re-downloading the compiler."
    """
    import shutil
    base = Path(_ESPHOME_VERSIONS_DIR)
    if not base.exists():
        logger.info("No build cache to clean (%s does not exist)", base)
        return
    removed: list[str] = []
    preserved: list[str] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        # #119: preserve any directory that's an ESPHome venv.
        if (entry / "bin" / "esphome").exists():
            preserved.append(entry.name)
            continue
        # #214: preserve PlatformIO core dirs so the toolchain isn't
        # re-downloaded on every Clean Cache. Slot/cache build
        # artifacts under ``pio-slot-<N>/cache/`` (PlatformIO's HTTP
        # cache, scratch builds) are tiny vs the packages tree, so
        # "preserve the whole pio-slot-N" is the right granularity.
        if entry.name.startswith("pio-slot-"):
            preserved.append(entry.name)
            continue
        try:
            shutil.rmtree(entry)
            removed.append(entry.name)
            logger.info("Removed %s", entry.name)
        except Exception as exc:
            logger.warning("Failed to remove %s: %s", entry.name, exc)
    if preserved:
        logger.info(
            "Build cache clean complete — removed %d cache dir(s); "
            "preserved %d (venvs + pio-slot-*): %s",
            len(removed), len(preserved), preserved,
        )
    else:
        logger.info(
            "Build cache clean complete — removed %d cache dir(s)",
            len(removed),
        )


# ---------------------------------------------------------------------------
# DQ.9: disk-quota sweep — runs at startup, between jobs, and after a
# heartbeat brings a lower set_disk_quota_bytes. Logs one tidy summary
# line per sweep ("evicted: 0 orphans, 1 stale venv, ...") so the user
# can spot eviction churn in the worker log without trawling per-file
# logs.
# ---------------------------------------------------------------------------


def _run_disk_quota_sweep(
    *,
    label: str,
    prune_orphans_first: bool = False,
) -> None:
    """Run :func:`disk_quota.enforce_quota` (and optionally :func:`prune_orphans`)
    against the current effective quota.

    Safe to call from any thread. No-op when no quota is in effect (e.g.
    a worker that hasn't received its first heartbeat yet — the sweep
    will run on the next trigger). Updates ``_last_eviction_freed_bytes``
    so the next heartbeat surfaces the result via ``system_info``.
    """
    import disk_quota  # noqa: PLC0415

    quota = _get_current_disk_quota_bytes()
    if quota is None:
        # Pre-first-heartbeat OR explicit "no override; server hasn't replied
        # yet" — skip silently. We'll catch up on the next trigger.
        return

    base = Path(_ESPHOME_VERSIONS_DIR)
    if not base.exists():
        return

    pinned = _active_job_set.snapshot()
    total_freed = 0
    summary_parts: list[str] = []

    if prune_orphans_first:
        orphan_result = disk_quota.prune_orphans(base, max_slots=MAX_PARALLEL_JOBS)
        total_freed += orphan_result.freed_bytes
        if orphan_result.orphan_slots_evicted:
            summary_parts.append(f"{orphan_result.orphan_slots_evicted} orphans")

    quota_result = disk_quota.enforce_quota(base, quota, pinned=pinned)
    total_freed += quota_result.freed_bytes
    if quota_result.venvs_evicted:
        summary_parts.append(f"{quota_result.venvs_evicted} stale venv(s)")
    if quota_result.targets_evicted:
        summary_parts.append(f"{quota_result.targets_evicted} target(s)")
    if quota_result.pio_slots_evicted:
        summary_parts.append(f"{quota_result.pio_slots_evicted} pio-slot(s)")

    floor_result = disk_quota.host_disk_floor(base, MIN_FREE_DISK_PCT, pinned=pinned)
    total_freed += floor_result.freed_bytes
    if floor_result.freed_bytes > 0:
        summary_parts.append(f"+{floor_result.freed_bytes} B (host-disk floor)")

    _record_eviction_freed_bytes(total_freed)

    usage = disk_quota.compute_usage(base).total_bytes
    summary = ", ".join(summary_parts) if summary_parts else "nothing"
    logger.info(
        "disk-quota[%s]: %.1f / %.1f GiB — evicted: %s",
        label,
        usage / 1024 ** 3,
        quota / 1024 ** 3,
        summary,
    )


# ---------------------------------------------------------------------------
# WL.2: pull-when-watched log pusher
# ---------------------------------------------------------------------------

# Set by the control-poll / heartbeat loops whenever the server reports
# ``stream_logs=True``; cleared on False. Read by the pusher thread.
_stream_logs_event = threading.Event()

# Byte-offset the next pusher push should start from. Module-scoped so
# it survives the pusher thread exiting when a user closes the dialog —
# otherwise a close+reopen would respawn the pusher with
# ``acked_offset=0`` and re-send the whole ring buffer. The server
# treats that as a worker restart (offset=0 after next_offset>0), emits
# its "--- worker restarted ---" separator, and the UI sees every line
# twice.
_log_push_acked_offset = 0
_log_push_lock = threading.Lock()

# Guards the check-then-spawn in _update_log_streaming so the control-
# poll thread and the heartbeat thread can't race into spawning two
# pushers.
_streaming_start_lock = threading.Lock()

# #109: id of the diagnostics request currently being serviced (or
# just handed off to a worker thread). The heartbeat and the 1-Hz
# control poll both see the same request id for up to 10 s until the
# server clears the pending slot, so we dedupe on this to avoid
# running `py-spy dump` three times for one click.
_diagnostics_in_flight: set[str] = set()
_diagnostics_in_flight_lock = threading.Lock()


def _log_pusher_loop(client_id: str, stop_event: threading.Event) -> None:
    """Drain the LogCaptureHandler ring and POST chunks to the server.

    Runs while ``_stream_logs_event`` is set. Exits cleanly when either
    that event clears (server told us to stop) or ``stop_event`` is set
    (process shutdown).

    Acked offset lives in the module-level ``_log_push_acked_offset``
    so a dialog close+reopen picks up where the previous pusher left
    off — no re-sending chunks the server already has.

    Retry policy on transport error: do NOT advance the ack; the next
    tick will re-send from the same point. Lines never drop on happy
    + 5xx paths.
    """
    global _log_push_acked_offset
    while _stream_logs_event.is_set() and not stop_event.is_set():
        with _log_push_lock:
            acked_offset = _log_push_acked_offset
        chunk, new_offset = _log_capture.drain_since(acked_offset)
        if chunk:
            try:
                resp = post(
                    f"/api/v1/workers/{client_id}/logs",
                    WorkerLogAppend(offset=acked_offset, lines=chunk).model_dump(),
                    timeout=10,
                )
                if resp.ok:
                    with _log_push_lock:
                        _log_push_acked_offset = new_offset
                # else: transient server-side error, retry next tick.
            except requests.RequestException:
                # Network hiccup — retry next tick without advancing.
                pass
        # 1 Hz push cadence matches the approved design; fast enough
        # to feel like tail -f without generating much traffic.
        stop_event.wait(1.0)


def _control_poll_loop(client_id: str, stop_event: threading.Event) -> None:
    """Poll /api/v1/workers/{id}/control at 1 Hz for fast watch-state updates.

    The heartbeat also carries ``stream_logs`` but runs every 10 s — too
    slow for a "tail -f" UX. This lightweight GET (body is just
    ``{"stream_logs": bool}``) lets the worker react within ~1 s of a
    UI user opening or closing the log dialog.
    """
    while not stop_event.is_set():
        try:
            resp = get(f"/api/v1/workers/{client_id}/control", timeout=5)
            if resp.ok:
                data = resp.json()
                stream_logs = data.get("stream_logs")
                if isinstance(stream_logs, bool):
                    _update_log_streaming(client_id, stream_logs, stop_event)
                # #109: fast-path diagnostics pickup — same request id
                # rides both the heartbeat (every 10 s) and this 1 Hz
                # poll, so a "Request diagnostics" click lands a dump
                # in the UI within a second or two.
                diag_req = data.get("diagnostics_request_id")
                if isinstance(diag_req, str) and diag_req:
                    _maybe_handle_diagnostics_request(client_id, diag_req)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            # Same reachability dedup as heartbeat — don't spam warnings
            # when the server is briefly unavailable. Next tick retries.
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("control-poll unexpected error: %s", exc)
        stop_event.wait(1.0)


def _maybe_handle_diagnostics_request(client_id: str, request_id: str) -> None:
    """Fire-and-forget a worker thread that runs ``py-spy dump --pid
    <self>`` and POSTs the result. Deduplicated on ``request_id`` so
    the heartbeat + 1-Hz control poll firing the same id don't trigger
    parallel dumps (#109).
    """
    with _diagnostics_in_flight_lock:
        if request_id in _diagnostics_in_flight:
            return
        _diagnostics_in_flight.add(request_id)

    def _runner() -> None:
        try:
            ok, dump = _produce_thread_dump()
            try:
                payload = WorkerDiagnosticsUpload(
                    request_id=request_id, ok=ok, dump=dump,
                ).model_dump(exclude_none=True)
                resp = post(
                    f"/api/v1/workers/{client_id}/diagnostics",
                    payload,
                    timeout=30,
                )
                if not resp.ok:
                    logger.warning(
                        "diagnostics upload failed: HTTP %s — %s",
                        resp.status_code, resp.text[:200],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("diagnostics upload unexpected error: %s", exc)
        finally:
            with _diagnostics_in_flight_lock:
                _diagnostics_in_flight.discard(request_id)

    threading.Thread(target=_runner, name="diag-upload", daemon=True).start()


def _in_process_thread_dump() -> str:
    """Walk every live Python thread's stack and format the result as
    plain text. Mirror of ``diagnostics.in_process_thread_dump`` on
    the server side — duplicated here because the client image
    doesn't include server-only modules (same reason protocol.py has
    two byte-identical copies).

    Pure-Python frame walk via :func:`sys._current_frames` and
    :func:`threading.enumerate`: no ``py-spy``, no subprocess, no
    ``ptrace``. Works under any container constraints (HA add-on with
    dropped ``CAP_SYS_PTRACE``, HAOS / Supervised sidecars, etc.) —
    added in #189, supersedes #108's py-spy-subprocess path.
    """
    import platform  # noqa: PLC0415 — lazy import, only on diagnostics path
    import traceback  # noqa: PLC0415

    frames = sys._current_frames()
    threads_by_ident = {t.ident: t for t in threading.enumerate()}

    lines: list[str] = [
        "Fleet for ESPHome worker thread dump",
        f"Process: pid={os.getpid()}  v{CLIENT_VERSION}  image={IMAGE_VERSION}",
        f"Python {platform.python_version()} on {sys.platform}",
        f"{len(frames)} thread(s)",
        "",
    ]
    for tid, frame in frames.items():
        thread = threads_by_ident.get(tid)
        if thread is not None:
            name = thread.name
            daemon = "daemon=True" if thread.daemon else "daemon=False"
        else:
            name = "<unknown>"
            daemon = "daemon=?"
        lines.append(f'Thread {tid} "{name}" ({daemon}):')
        for chunk in traceback.format_stack(frame):
            for subline in chunk.rstrip("\n").splitlines():
                lines.append(f"  {subline}")
        lines.append("")
    return "\n".join(lines)


def _produce_thread_dump() -> tuple[bool, str]:
    """Capture a thread dump of this worker's own process. Returns
    ``(ok, text)``; ``ok`` is always True on this code path since the
    in-process frame walk can't fail under container constraints.
    The bool is retained in the signature for protocol compatibility
    with :class:`WorkerDiagnosticsUpload`."""
    return True, _in_process_thread_dump()


def _update_log_streaming(client_id: str, stream_logs: Optional[bool],
                          stop_event: threading.Event) -> None:
    """Start or stop the pusher thread on watch-state transitions.

    Called from both the control-poll loop (1 Hz) and the heartbeat
    loop (10 s). None means "unchanged — default state pre-WL, absent
    field in response"; only explicit True/False drive state changes.

    Guarded by ``_streaming_start_lock`` so the two callers can't race
    into spawning two pusher threads for the same transition.
    """
    if stream_logs is None:
        return
    with _streaming_start_lock:
        if stream_logs and not _stream_logs_event.is_set():
            _stream_logs_event.set()
            t = threading.Thread(
                target=_log_pusher_loop,
                args=(client_id, stop_event),
                name="log-pusher",
                daemon=True,
            )
            t.start()
        elif not stream_logs and _stream_logs_event.is_set():
            _stream_logs_event.clear()


def heartbeat_loop(client_id: str, stop_event: threading.Event) -> None:
    """Send heartbeats to the server until stop_event is set."""
    global _image_upgrade_logged
    while not stop_event.is_set():
        try:
            hb = HeartbeatRequest(
                client_id=client_id,
                system_info=_build_system_info(),
            )
            resp = post("/api/v1/workers/heartbeat", hb.model_dump(exclude_none=True))
            if resp.status_code == 401:
                _on_auth_failed()
            elif resp.status_code == 404:
                # Server doesn't recognise us — signal main loop to re-register.
                # Log only on the first occurrence; the main loop will clear this.
                if not _reregister_needed.is_set():
                    logger.warning("Server does not know us; will re-register")
                _reregister_needed.set()
            elif resp.ok:
                _on_server_reachable()
                _on_auth_ok()
                try:
                    data = HeartbeatResponse.model_validate(resp.json())
                except ValidationError as exc:
                    logger.warning("Malformed heartbeat response: %s", exc)
                    stop_event.wait(HEARTBEAT_INTERVAL)
                    continue
                # Server may refuse source-code auto-updates if our Docker image
                # is too old to safely receive them (missing system deps, etc.)
                if data.image_upgrade_required:
                    min_v = data.min_image_version or "?"
                    if not _image_upgrade_logged:
                        logger.warning(
                            "Docker image upgrade required: this worker reports IMAGE_VERSION=%s "
                            "but the server's MIN_IMAGE_VERSION=%s. Auto-updates are disabled "
                            "until the Docker image is rebuilt with `docker pull` + restart.",
                            IMAGE_VERSION or "<none>", min_v,
                        )
                        _image_upgrade_logged = True
                else:
                    sv = data.server_client_version
                    if sv and sv != CLIENT_VERSION:
                        logger.info(
                            "Worker update available: local=%s server=%s", CLIENT_VERSION, sv
                        )
                        _update_available.set()
                # DQ.9: server pushes the effective disk quota on every
                # heartbeat. Lower-quota transitions trigger an immediate
                # sweep so the worker doesn't keep sitting on bytes the
                # operator just told it to evict.
                new_quota = data.set_disk_quota_bytes
                if new_quota is not None:
                    prev_quota = _get_current_disk_quota_bytes()
                    if prev_quota != new_quota:
                        _set_current_disk_quota_bytes(new_quota)
                        if prev_quota is None:
                            # #ef-a5l: first heartbeat to bring a quota.
                            # Worker may have booted over budget — startup
                            # sweep no-op'd because quota was None then.
                            # Sweep now rather than waiting for the next
                            # successful compile.
                            try:
                                _run_disk_quota_sweep(label="initial-quota")
                            except Exception:
                                logger.exception("disk-quota: sweep on initial quota failed")
                        elif new_quota < prev_quota:
                            try:
                                _run_disk_quota_sweep(label="quota-lowered")
                            except Exception:
                                logger.exception("disk-quota: sweep on lower quota failed")
                # Check for max_parallel_jobs config change from UI
                new_jobs = data.set_max_parallel_jobs
                if new_jobs is not None and new_jobs != MAX_PARALLEL_JOBS:
                    logger.info(
                        "Server requested max_parallel_jobs change: %d → %d — restarting",
                        MAX_PARALLEL_JOBS, new_jobs,
                    )
                    # Write new value to env so it persists across restart
                    os.environ["MAX_PARALLEL_JOBS"] = str(new_jobs)
                    _restart_self()
                # Bug #4: defer the clean until all in-flight compiles finish.
                # Cleaning mid-compile wipes the .esphome/build/* directory the
                # running ``esphome run`` is mid-way through writing, which
                # silently breaks the upload. Set a pending flag here; the
                # main loop drains active jobs and runs the actual clean
                # below at idle. Pollers also skip claiming new jobs while
                # this is set so the worker reaches quiescence.
                if data.clean_build_cache:
                    if _is_idle():
                        logger.info("Server requested build cache clean — worker is idle, clearing immediately")
                        _clean_build_cache()
                    else:
                        if not _clean_pending.is_set():
                            logger.info(
                                "Server requested build cache clean — deferring until "
                                "active job(s) finish",
                            )
                        _clean_pending.set()
                # WL.2: start/stop the log pusher thread based on the
                # server's watch state. The heartbeat is the single
                # signal source; pusher never polls the server for
                # this flag on its own.
                _update_log_streaming(client_id, data.stream_logs, stop_event)
                # #109: the UI asked us for a thread dump. Run py-spy
                # on our own PID and POST the result. Done off-thread
                # so a stuck py-spy (or a long dump) doesn't stall
                # heartbeat cadence.
                if data.diagnostics_request_id is not None:
                    _maybe_handle_diagnostics_request(client_id, data.diagnostics_request_id)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            _on_server_unreachable(exc)
        except Exception as exc:
            logger.warning("Heartbeat unexpected error: %s", exc)
        stop_event.wait(HEARTBEAT_INTERVAL)


# ---------------------------------------------------------------------------
# Bundle extraction
# ---------------------------------------------------------------------------

def extract_bundle(bundle_b64: str, dest_dir: str) -> None:
    """Decode and extract the base64 tar.gz bundle into dest_dir."""
    raw = base64.b64decode(bundle_b64)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        try:
            tar.extractall(path=dest_dir, filter="data")
        except TypeError:
            tar.extractall(path=dest_dir)  # Python < 3.12
    logger.debug("Bundle extracted to %s", dest_dir)


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

_update_attempts: int = 0
_MAX_UPDATE_ATTEMPTS: int = 3


def _apply_update(current_client_id: str) -> None:
    """Download updated worker code from server and restart the process.

    Stashes *current_client_id* in the environment so the restarted process
    can re-register in place (keeping the same entry in the server's registry).
    """
    global _update_attempts
    _update_available.clear()
    _update_attempts += 1
    if _update_attempts > _MAX_UPDATE_ATTEMPTS:
        logger.warning(
            "Update failed %d times; giving up until restart", _MAX_UPDATE_ATTEMPTS
        )
        return
    logger.info("Downloading worker update from server...")
    try:
        resp = get("/api/v1/client/code", timeout=60)
        resp.raise_for_status()
        data = resp.json()
        files = data.get("files", {})
        new_version = data.get("version", "?")
        if not files:
            logger.warning("Update response had no files; skipping")
            return
        client_dir = Path(__file__).parent.resolve()
        for filename, content in files.items():
            if not filename.endswith(".py"):
                continue
            target = (client_dir / filename).resolve()
            if target.parent != client_dir:
                logger.warning("Skipping suspicious path in update: %s", filename)
                continue
            target.write_text(content, encoding="utf-8")
            logger.info("Updated %s", filename)
        logger.info("Worker updated to %s — restarting", new_version)
        os.environ["DISTRIBUTED_ESPHOME_CLIENT_ID"] = current_client_id
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        logger.warning("Worker update failed: %s", exc)


def _ota_network_diagnostics(target_path: str, cwd: str, env: dict) -> str:
    """Run network diagnostics after an OTA failure.

    Parses the target YAML (best-effort) to find the device IP/hostname,
    then checks TCP connectivity, DNS resolution, and network route.
    Returns a human-readable diagnostics string for the build log.
    """
    import re as _re  # noqa: PLC0415

    lines: list[str] = []

    # Try to find the device address from the YAML config.
    # Priority order (matching ESPHome's own logic):
    #   1. wifi.use_address (explicit override)
    #   2. wifi.manual_ip.static_ip
    #   3. DNS resolution of device name
    device_addr = None
    ota_port = None
    try:
        with open(target_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        # use_address takes priority — ESPHome uses this as the upload target
        use_addr_match = _re.search(r"use_address:\s*['\"]?([^\s'\"#]+)", content)
        if use_addr_match:
            device_addr = use_addr_match.group(1)
            lines.append(f"use_address: {device_addr}")
        # Fall back to static_ip
        if not device_addr:
            ip_match = _re.search(r"static_ip:\s*['\"]?(\d+\.\d+\.\d+\.\d+)", content)
            if ip_match:
                device_addr = ip_match.group(1)
        # Check for OTA port override
        port_match = _re.search(r"port:\s*(\d+)", content.split("ota:")[1] if "ota:" in content else "")
        if port_match:
            ota_port = int(port_match.group(1))
    except Exception:
        logger.debug("Could not parse device address/port from YAML %s", target_path, exc_info=True)

    # Extract device name from the esphome: block (not any other component's name: key).
    # Parse with yaml.safe_load to avoid the regex pitfall of matching the wrong name:.
    device_name = None
    try:
        import yaml as _yaml  # noqa: PLC0415
        with open(target_path, encoding="utf-8", errors="replace") as f:
            raw = _yaml.safe_load(f)
        if isinstance(raw, dict):
            esphome_block = raw.get("esphome") or {}
            if isinstance(esphome_block, dict) and esphome_block.get("name"):
                device_name = str(esphome_block["name"])
    except Exception:
        # Fallback: look for name: directly under an esphome: line
        try:
            with open(target_path, encoding="utf-8", errors="replace") as f:
                content_lines = f.readlines()
            in_esphome = False
            for line in content_lines:
                stripped = line.lstrip()
                # Top-level key (no indent) — check if it's esphome:
                if line and not line[0].isspace() and stripped.startswith("esphome:"):
                    in_esphome = True
                    continue
                elif line and not line[0].isspace():
                    in_esphome = False
                    continue
                if in_esphome:
                    m = _re.match(r'\s+name:\s*["\']?([a-zA-Z0-9_-]+)', line)
                    if m:
                        device_name = m.group(1)
                        break
        except Exception:
            logger.debug("Could not extract device name from YAML %s", target_path, exc_info=True)

    # If use_address is a hostname (not IP), try to resolve it
    if device_addr and not _re.match(r'\d+\.\d+\.\d+\.\d+$', device_addr):
        hostname = device_addr
        try:
            import socket as _socket  # noqa: PLC0415
            device_addr = _socket.gethostbyname(hostname)
            lines.append(f"Resolved {hostname} → {device_addr}")
        except Exception:
            lines.append(f"DNS: {hostname} — FAILED to resolve")
            device_addr = None

    if not device_addr and device_name:
        # Try DNS resolution of the device name (ESPHome devices register as <name>.local)
        try:
            import socket as _socket  # noqa: PLC0415
            device_addr = _socket.gethostbyname(f"{device_name}.local")
            lines.append(f"Resolved {device_name}.local → {device_addr}")
        except Exception:
            lines.append(f"DNS: {device_name}.local — FAILED to resolve")
            # Try without .local
            try:
                device_addr = _socket.gethostbyname(device_name)
                lines.append(f"Resolved {device_name} → {device_addr}")
            except Exception:
                lines.append(f"DNS: {device_name} — FAILED to resolve")

    if not device_addr:
        lines.append("Could not determine device IP for diagnostics")
        return "\n".join(lines)

    # Determine OTA port: ESP8266 uses 8266, ESP32 uses 3232
    if not ota_port:
        # Check both common ports
        ports_to_check = [3232, 8266]
    else:
        ports_to_check = [ota_port]

    lines.append(f"Device IP: {device_addr}")

    # TCP connectivity check
    import socket as _socket  # noqa: PLC0415
    for port in ports_to_check:
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((device_addr, port))
            if result == 0:
                lines.append(f"TCP {device_addr}:{port} — OPEN (connected)")
            else:
                lines.append(f"TCP {device_addr}:{port} — CLOSED (errno {result})")
            sock.close()
        except _socket.timeout:
            lines.append(f"TCP {device_addr}:{port} — TIMEOUT (5s)")
        except Exception as exc:
            lines.append(f"TCP {device_addr}:{port} — ERROR: {exc}")

    # Ping check (ICMP). Bug #6 (1.6.1): iputils-ping is installed in
    # the worker image; if a third-party image strips it out we still
    # want a legible diagnostic line rather than a raw errno-2 traceback.
    try:
        ping_result = subprocess.run(
            ["ping", "-c", "3", "-W", "2", device_addr],
            capture_output=True, text=True, timeout=10,
        )
        ping_summary = [ln for ln in ping_result.stdout.splitlines() if "packet" in ln.lower() or "rtt" in ln.lower() or "round-trip" in ln.lower()]
        for line in ping_summary:
            lines.append(f"Ping: {line.strip()}")
        if ping_result.returncode != 0 and not ping_summary:
            lines.append(f"Ping: {device_addr} — UNREACHABLE")
    except FileNotFoundError:
        lines.append("Ping: skipped (no `ping` binary in this image)")
    except Exception as exc:
        lines.append(f"Ping: {exc}")

    # Check our own IP / network interface
    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.connect((device_addr, 80))
        our_ip = sock.getsockname()[0]
        sock.close()
        lines.append(f"Worker IP: {our_ip} (source for reaching {device_addr})")
    except Exception:
        logger.debug("Could not determine worker IP for OTA diagnostics", exc_info=True)

    # Docker network check
    try:
        if os.path.exists("/.dockerenv"):
            lines.append("Running inside Docker container")
            # Check if we're using host networking
            try:
                with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as f:
                    cgroup = f.read()
                if "docker" in cgroup:
                    lines.append("Network mode: bridge (NAT) — consider --network host if OTA fails consistently")
            except Exception:
                logger.debug("Could not read /proc/1/cgroup for Docker network check", exc_info=True)
    except Exception:
        logger.debug("Docker environment check failed", exc_info=True)

    diag_text = "\n".join(lines)
    logger.info("OTA diagnostics for %s:\n%s", device_addr, diag_text)
    return diag_text


# ---------------------------------------------------------------------------
# #45: Per-slot working dirs + shared per-target compile cache.
#
# Two concurrent compiles for the same target used to share one build dir
# under /esphome-versions/builds/<stem>/, racing on PlatformIO's .pio/ files
# and ESPHome's .esphome/ state. The fix:
#
#   /esphome-versions/
#     slots/<slot>/<stem>/   per-slot, per-target working dir (compile here)
#     cache/<stem>/          shared per-target cache of .pio/ + .esphome/
#     cache/<stem>.lock      fcntl lock — serialises rsync in/out per target
#
# Sync-in: only when the slot dir has no .pio/ yet (first compile of this
# target on this slot). Sync-out: always on successful compile, so any other
# slot that later picks up the same target gets a warm cache to start from.
# Both sync operations take the per-target lock.
# ---------------------------------------------------------------------------


def _slot_dir(worker_id: int, target_stem: str) -> str:
    return os.path.join(_ESPHOME_VERSIONS_DIR, "slots", str(worker_id), target_stem)


def _cache_dir(target_stem: str) -> str:
    return os.path.join(_ESPHOME_VERSIONS_DIR, "cache", target_stem)


@contextmanager
def _target_cache_lock(target_stem: str) -> Iterator[None]:
    """Exclusive fcntl lock on a per-target lock file under the cache dir.

    Serialises sync-in/sync-out for a target across slots so two workers
    can't step on each other while rsync'ing the .pio/.esphome subtrees.
    The lock file itself is never deleted — it's just a handle.
    """
    cache_parent = os.path.join(_ESPHOME_VERSIONS_DIR, "cache")
    os.makedirs(cache_parent, exist_ok=True)
    lock_path = os.path.join(cache_parent, f"{target_stem}.lock")
    with open(lock_path, "w", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def _copytree_replace(src: str, dst: str) -> None:
    """Copy *src* tree to *dst*, replacing *dst* if it exists.

    Uses shutil.rmtree + shutil.copytree — acceptable for typical ESPHome
    .pio/.esphome sizes (~50-100MB). Silently tolerates missing src.
    """
    if not os.path.isdir(src):
        return
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


def _sync_cache_into_slot(target_stem: str, slot_dir: str) -> None:
    """On first compile of *target_stem* in *slot_dir*, seed .pio/.esphome
    from the shared cache so the slot benefits from any prior compile on
    any other slot.
    """
    cache_dir = _cache_dir(target_stem)
    if not os.path.isdir(cache_dir):
        return

    slot_pio = os.path.join(slot_dir, ".pio")
    slot_esphome = os.path.join(slot_dir, ".esphome")
    cache_pio = os.path.join(cache_dir, ".pio")
    cache_esphome = os.path.join(cache_dir, ".esphome")

    need_pio = os.path.isdir(cache_pio) and not os.path.isdir(slot_pio)
    need_esphome = os.path.isdir(cache_esphome) and not os.path.isdir(slot_esphome)
    if not (need_pio or need_esphome):
        has_local = os.path.isdir(slot_pio) or os.path.isdir(slot_esphome)
        logger.info(
            "Slot cache sync-in skipped for %s (local=%s, shared=%s)",
            target_stem,
            "has .pio" if has_local else "empty",
            "present" if os.path.isdir(cache_dir) else "absent",
        )
        return

    with _target_cache_lock(target_stem):
        if need_pio:
            logger.info("Slot seeding .pio/ from shared cache for %s", target_stem)
            _copytree_replace(cache_pio, slot_pio)
        if need_esphome:
            logger.info("Slot seeding .esphome/ from shared cache for %s", target_stem)
            _copytree_replace(cache_esphome, slot_esphome)


def _sync_slot_into_cache(target_stem: str, slot_dir: str) -> None:
    """After a successful compile, push .pio/.esphome back to the shared
    cache so subsequent compiles on any slot start warm.
    """
    slot_pio = os.path.join(slot_dir, ".pio")
    slot_esphome = os.path.join(slot_dir, ".esphome")
    if not (os.path.isdir(slot_pio) or os.path.isdir(slot_esphome)):
        return

    cache_dir = _cache_dir(target_stem)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with _target_cache_lock(target_stem):
            if os.path.isdir(slot_pio):
                _copytree_replace(slot_pio, os.path.join(cache_dir, ".pio"))
            if os.path.isdir(slot_esphome):
                _copytree_replace(slot_esphome, os.path.join(cache_dir, ".esphome"))
        logger.info("Updated shared cache for %s", target_stem)
    except Exception:
        logger.warning("Failed to update shared cache for %s", target_stem, exc_info=True)


def run_job(client_id: str, job: dict, version_manager: VersionManager, worker_id: int = 1) -> None:
    """Execute a single build job end-to-end."""
    global _active_jobs
    with _active_jobs_lock:
        _active_jobs += 1
    job_id = job["job_id"]
    target = job["target"]
    esphome_version = job["esphome_version"]
    bundle_b64 = job["bundle_b64"]
    timeout_seconds = job.get("timeout_seconds", JOB_TIMEOUT)
    ota_only = job.get("ota_only", False)
    validate_only = job.get("validate_only", False)
    download_only = job.get("download_only", False)
    # SOTA.1: server performs OTA — worker compiles and uploads binary only.
    # Reuses the download_only path exactly; server handles the actual flash.
    server_ota = job.get("server_ota", False)
    if server_ota:
        download_only = True

    _log_context.current_target = target
    logger.info(
        "Starting job %s: target=%s esphome=%s ota_only=%s validate_only=%s download_only=%s server_ota=%s",
        job_id, target, esphome_version, ota_only, validate_only, download_only, server_ota,
    )

    # Per-slot PlatformIO core directory — prevents cross-slot package conflicts
    # when multiple workers run esphome compile simultaneously.
    pio_dir = os.path.join(_ESPHOME_VERSIONS_DIR, f"pio-slot-{worker_id}")
    try:
        os.makedirs(pio_dir, exist_ok=True)
        subprocess_env = {**os.environ, "PLATFORMIO_CORE_DIR": pio_dir}
        logger.debug("Worker %d using PLATFORMIO_CORE_DIR=%s", worker_id, pio_dir)
    except OSError as exc:
        logger.debug("Could not create pio dir %s (%s); using default PLATFORMIO_CORE_DIR", pio_dir, exc)
        subprocess_env = dict(os.environ)

    # Match server timezone so ESPHome produces identical config_hash.
    # Mismatched TZ → different hash → unnecessary clean rebuild → different firmware binary.
    server_tz = job.get("server_timezone")
    if server_tz:
        subprocess_env["TZ"] = server_tz
        logger.debug("Using server timezone: %s", server_tz)

    # Network timeouts for uv/pip during ESPHome's penv bootstrap. Defaults are
    # aggressive (uv HTTP read = 30s, pip socket = 15s) and cause intermittent
    # "Failed to install Python dependencies into penv" failures on slow or
    # flaky links — see GitHub #6. setdefault lets operators override via the
    # worker env if needed. Both PIP_DEFAULT_TIMEOUT and PIP_TIMEOUT map to
    # pip's --timeout option (verified in pip source).
    subprocess_env.setdefault("UV_HTTP_TIMEOUT", "180")
    subprocess_env.setdefault("UV_HTTP_CONNECT_TIMEOUT", "30")
    subprocess_env.setdefault("PIP_DEFAULT_TIMEOUT", "180")

    # Install ESPHome version (BEFORE starting the timeout timer)
    if ESPHOME_BIN:
        esphome_bin = ESPHOME_BIN
        logger.info("Using esphome binary override: %s", esphome_bin)
    else:
        _report_status(job_id, f"Preparing ESPHome {esphome_version}")
        _flush_log_text(job_id, f"Ensuring ESPHome {esphome_version} is available...\n")
        try:
            esphome_bin = version_manager.ensure_version(esphome_version)
            logger.info("Using esphome binary: %s", esphome_bin)
            _flush_log_text(job_id, f"ESPHome {esphome_version} ready.\n")
        except Exception as exc:
            error_detail = str(exc)
            logger.error("Failed to install esphome==%s: %s", esphome_version, error_detail)
            # Stream the full error to the job log so the user sees it in the terminal
            _flush_log_text(job_id, f"\n\033[31mERROR: Failed to install ESPHome {esphome_version}\033[0m\n{error_detail}\n")
            _submit_result(job_id, "failed", log=None, ota_result=None)
            with _active_jobs_lock:
                _active_jobs -= 1
            return

    # #13: stable per-target build directory so the .esphome/ build cache
    #      (PlatformIO compiled objects) persists across jobs — turns a
    #      60-90s full compile into a 5-10s incremental build.
    # #45: now per-SLOT as well, so concurrent compiles on the same worker
    #      don't race on the same directory. The shared /cache/<stem>/ dir
    #      is synced in on first compile and synced out on success so cache
    #      still reuses across slots via the shared cache. Two slots can
    #      compile the same target in parallel without stepping on each
    #      other — they work in separate slot dirs and only contend on the
    #      brief sync-in/sync-out phases (serialized by a per-target lock).
    target_stem = os.path.splitext(target)[0]
    build_dir = _slot_dir(worker_id, target_stem)
    os.makedirs(build_dir, exist_ok=True)
    # DQ.9: pin the venv + target stem + slot for the duration of the
    # job so the disk-quota engine doesn't evict files we're using.
    # Refcounted so two parallel jobs on the same target both protect it.
    pin_cm = _active_job_set.pin(esphome_version, target_stem, worker_id)
    pin_cm.__enter__()
    try:
        # Seed this slot's .pio/.esphome from the shared cache if this is
        # the first compile of this target on this slot. No-op otherwise.
        _sync_cache_into_slot(target_stem, build_dir)
        # Extract bundle into the stable dir (overwrites changed files;
        # .esphome/ subdir with PlatformIO cache is preserved).
        try:
            extract_bundle(bundle_b64, build_dir)
        except Exception as exc:
            logger.error("Bundle extraction failed: %s", exc)
            _submit_result(job_id, "failed", log=f"Bundle extraction failed: {exc}", ota_result=None)
            return

        target_path = os.path.join(build_dir, target)
        if not os.path.exists(target_path):
            _submit_result(job_id, "failed", log=f"Target file not found in bundle: {target}", ota_result=None)
            return

        # ---------------------------------------------------------------
        # Validation phase (validate_only=True) — runs esphome config and exits
        # ---------------------------------------------------------------
        if validate_only:
            _report_status(job_id, "Validating")
            validate_cmd = [esphome_bin, "config", target_path]
            _log_invocation(job_id, validate_cmd)
            _compile_log, compile_ok = _run_subprocess(
                validate_cmd,
                cwd=build_dir,
                timeout=60,  # validation is fast — 60s is plenty
                label="validate",
                env=subprocess_env,
                job_id=job_id,
            )
            _submit_result(job_id, "success" if compile_ok else "failed", log=None, ota_result=None)
            return  # skip compile and OTA phases

        # ---------------------------------------------------------------
        # Compile-and-download phase (download_only=True) — runs
        # `esphome compile` (no OTA), locates the produced firmware .bin
        # under .esphome/build/<device>/.pioenvs/<device>/, POSTs it to
        # the server, and reports success. FD.4.
        # ---------------------------------------------------------------
        if download_only:
            _report_status(job_id, "Compiling (no OTA)")
            compile_cmd = [esphome_bin, "compile", target_path]
            _log_invocation(job_id, compile_cmd)
            compile_log, compile_ok = _run_subprocess(
                compile_cmd,
                cwd=build_dir,
                timeout=timeout_seconds,
                label="compile",
                env=subprocess_env,
                job_id=job_id,
            )
            # #214: same self-heal as the OTA path — see the comment on
            # the run_cmd retry block below for context.
            if not compile_ok and _is_broken_pio_state(compile_log):
                _log_toolchain_state(pio_dir, "download_only compile failed with broken pio-slot signature")
                if _wipe_broken_toolchain(pio_dir):
                    _flush_log_text(
                        job_id,
                        "\n--- #214 self-heal: PlatformIO state was broken. "
                        "Wiped pio-slot/packages/ + penv/ and retrying compile. ---\n",
                    )
                    compile_log, compile_ok = _run_subprocess(
                        compile_cmd,
                        cwd=build_dir,
                        timeout=timeout_seconds,
                        label="compile (retry after pio-slot wipe)",
                        env=subprocess_env,
                        job_id=job_id,
                    )
            if not compile_ok:
                _submit_result(job_id, "failed", log=None, ota_result=None)
                return
            # Compile succeeded — warm the shared cache.
            _sync_slot_into_cache(target_stem, build_dir)

            # #69 + Bug #9: collect every variant (factory + ota on ESP32;
            # ota only on ESP8266) and archive it on the server. For a
            # download-only job the upload is the whole point — a failure
            # fails the job. The shared helper is also used by the OTA
            # path below (best-effort) so both shapes write to the same
            # /data/firmware/ directory on the server.
            if not _archive_firmware_to_server(
                job_id, build_dir, target_stem,
                client_id=client_id, required=True,
            ):
                _submit_result(job_id, "failed", log=None, ota_result=None)
                return
            _submit_result(job_id, "success", log=None, ota_result=None)
            return

        # ---------------------------------------------------------------
        # Build + OTA via `esphome run` (compile and upload in one step)
        #
        # --no-logs is REQUIRED on `esphome run` so the worker doesn't hang
        # tailing device logs after a successful OTA. It is NOT accepted by
        # `esphome upload` — passing it to the retry path in bug #177 caused
        # the retry to crash with "unrecognized arguments: --no-logs".
        #
        # --device is ALWAYS set:
        #   - ota_address from the server if known (device poller has an IP)
        #   - otherwise the literal string "OTA", which tells ESPHome to
        #     resolve the device itself and skip the interactive upload
        #     target prompt (#176). Without this, ESPHome prompts when the
        #     worker has multiple possible targets (e.g. a USB serial dongle
        #     plus the OTA target), and the worker has no stdin.
        # ---------------------------------------------------------------
        ota_address = job.get("ota_address") or "OTA"

        _report_status(job_id, "Compiling + OTA" + (" (retry)" if ota_only else ""))
        run_cmd = [
            esphome_bin, "run", target_path,
            "--no-logs",
            "--device", ota_address,
        ]
        _log_invocation(job_id, run_cmd)

        # Total timeout covers both compile + OTA
        total_timeout = timeout_seconds + OTA_TIMEOUT
        run_log, run_ok = _run_subprocess(
            run_cmd,
            cwd=build_dir,
            timeout=total_timeout,
            label="compile+OTA",
            env=subprocess_env,
            job_id=job_id,
        )

        # #214 / #220: when the compile failed with any of the broken-
        # pio-slot signatures (see ``_BROKEN_PIO_SIGNATURES``), the
        # worker's ``pio-slot-N/`` tree is corrupted in steady state and
        # PlatformIO won't self-recover — so the worker has to: dump the
        # toolchain layout for the log, wipe ``packages/`` + ``penv/``,
        # and retry the compile once so the next attempt re-extracts
        # everything via curl/tar (~5–10 min on a cold link). The retry
        # happens in-process before we submit "failed", so the original
        # job succeeds on the second try and the operator never has to
        # babysit the worker manually.
        if not run_ok and _is_broken_pio_state(run_log):
            _log_toolchain_state(pio_dir, "compile failed with broken pio-slot signature")
            if _wipe_broken_toolchain(pio_dir):
                _flush_log_text(
                    job_id,
                    "\n--- #214 self-heal: PlatformIO state was broken. "
                    "Wiped pio-slot/packages/ + penv/ and retrying compile — "
                    "the re-extract takes 5–10 min on cold networks, so "
                    "subsequent jobs may queue behind this one. ---\n",
                )
                run_log, run_ok = _run_subprocess(
                    run_cmd,
                    cwd=build_dir,
                    timeout=total_timeout,
                    label="compile+OTA (retry after toolchain wipe)",
                    env=subprocess_env,
                    job_id=job_id,
                )

        if run_ok:
            # #45: compile succeeded — sync the slot's .pio/.esphome back to
            # the shared cache so other slots can start warm next time.
            _sync_slot_into_cache(target_stem, build_dir)
            # Bug #9 (1.6.1): archive the binary on the server as a
            # best-effort side-effect. The OTA has already completed
            # successfully, so an archive failure is a warning, not a
            # fatal error. Must run BEFORE _submit_result — the server
            # rejects firmware uploads for jobs whose state has left
            # WORKING.
            _archive_firmware_to_server(
                job_id, build_dir, target_stem,
                client_id=client_id, required=False,
            )
            _submit_result(job_id, "success", log=None, ota_result="success")
        else:
            log_lower = run_log.lower()
            compile_succeeded = "successfully compiled" in log_lower
            ota_failed = compile_succeeded and ("failed" in log_lower or "timed out" in log_lower)

            # #45: if the COMPILE succeeded (even if OTA failed or retried)
            # we still want to promote the build artifacts to the shared
            # cache — a successful compile is worth caching regardless of
            # whether the device was reachable for OTA.
            if compile_succeeded:
                _sync_slot_into_cache(target_stem, build_dir)

            if not compile_succeeded:
                _submit_result(job_id, "failed", log=None, ota_result=None)
            elif ota_failed:
                # Compile succeeded but OTA failed — retry OTA before reporting.
                # Keep job in WORKING state so timeout checker can re-queue if we die.
                # Note: `esphome upload` does NOT accept --no-logs (it never tails
                # device logs anyway), so this retry path only passes --device.
                _flush_log_text(job_id, "\n--- OTA failed, retrying in 5s ---\n")
                time.sleep(5)
                _report_status(job_id, "OTA Retry")
                upload_cmd = [
                    esphome_bin, "upload", target_path,
                    "--device", ota_address,
                ]
                _log_invocation(job_id, upload_cmd)
                retry_log, retry_ok = _run_subprocess(
                    upload_cmd,
                    cwd=build_dir,
                    timeout=OTA_TIMEOUT,
                    label="OTA retry",
                    env=subprocess_env,
                    job_id=job_id,
                )
                # Bug #9 (1.6.1): compile succeeded — regardless of the
                # OTA-retry outcome, archive the firmware on the server
                # so the user can still flash it by hand (or re-OTA
                # later) if the device is unreachable.
                _archive_firmware_to_server(
                    job_id, build_dir, target_stem,
                    client_id=client_id, required=False,
                )
                if retry_ok:
                    _submit_result(job_id, "success", log=None, ota_result="success")
                else:
                    _submit_result(job_id, "success", log=None, ota_result="failed")
                    diag = _ota_network_diagnostics(target_path, build_dir, subprocess_env)
                    if diag:
                        _flush_log_text(job_id, "\n--- Network Diagnostics ---\n" + diag)
            else:
                # Compile succeeded but something else failed — archive the
                # binary anyway so the user has a fallback.
                _archive_firmware_to_server(
                    job_id, build_dir, target_stem,
                    client_id=client_id, required=False,
                )
                _submit_result(job_id, "success", log=None, ota_result="failed")

    finally:
        _log_context.current_target = None
        with _active_jobs_lock:
            _active_jobs -= 1
        # DQ.9: drop the disk-quota pin first so the post-job sweep can
        # reclaim caches our slot will no longer touch. The exception
        # path is fine — pin_cm.__exit__ accepts the exc tuple shape
        # contextlib injected, but here we bail without exception data
        # and let the running exception propagate via the outer try.
        try:
            pin_cm.__exit__(None, None, None)
        except Exception:
            logger.exception("disk-quota: pin __exit__ raised")
        # DQ.9: post-job sweep — same trigger point as _sync_slot_into_cache
        # was already running. If the quota cell hasn't been seeded yet
        # (no heartbeat reply yet) the sweep is a no-op.
        try:
            _run_disk_quota_sweep(label="post-job")
        except Exception:
            logger.exception("disk-quota: post-job sweep failed")
        # #13: intentionally NOT cleaning up build_dir — the .esphome/
        # subdirectory contains PlatformIO's compiled object cache. Keeping
        # it turns a 60-90s full compile into a 5-10s incremental build.
        # The "Clean Cache" button in the Workers tab already handles
        # cleanup by removing all of /esphome-versions/ including builds/.


def _colorize_log_line(line: str) -> str:
    """Add ANSI color codes to ESPHome log lines based on level prefix."""
    stripped = line.lstrip()
    if stripped.startswith("INFO "):
        return f"\033[32m{line}\033[0m"  # green
    if stripped.startswith("WARNING "):
        return f"\033[33m{line}\033[0m"  # yellow/orange
    if stripped.startswith("ERROR "):
        return f"\033[31m{line}\033[0m"  # red
    return line


def _run_subprocess(
    cmd: list[str],
    cwd: str,
    timeout: int,
    label: str,
    env: Optional[dict] = None,
    job_id: Optional[str] = None,
) -> tuple[str, bool]:
    """
    Run a subprocess with a timeout, streaming output line-by-line.

    Returns (combined_log, success).
    On timeout, kills the process and returns (log + 'TIMED OUT', False).
    *env* is passed directly to Popen; defaults to inheriting the current env.
    *job_id* enables live log streaming — lines are batched and POSTed to the
    server every 2 seconds via ``/api/v1/jobs/{id}/log``.
    """
    FLUSH_INTERVAL = 0.5
    log_chunks: list[str] = []
    flush_buffer: list[str] = []
    last_flush = time.monotonic()
    timed_out = threading.Event()
    logger.info("Running %s: %s", label, " ".join(cmd))

    def _flush_log():
        nonlocal flush_buffer, last_flush
        if not job_id or not flush_buffer:
            return
        text = "".join(flush_buffer)
        flush_buffer = []
        last_flush = time.monotonic()
        try:
            post(f"/api/v1/jobs/{job_id}/log", {"lines": text}, timeout=5)
        except Exception:
            logger.debug("Log flush to server failed for job %s", job_id, exc_info=True)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
    except Exception as exc:
        return f"Failed to start process: {exc}", False

    def _kill_on_timeout():
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            logger.debug("Failed to kill timed-out subprocess for %s", label, exc_info=True)

    timer = threading.Timer(timeout, _kill_on_timeout)
    timer.start()
    try:
        # read1() returns whatever bytes are available immediately (no blocking
        # to fill a full buffer), so we flush output to the server promptly.
        assert proc.stdout is not None
        raw: Any = proc.stdout
        while True:
            chunk = raw.read1(8192) if hasattr(raw, 'read1') else raw.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            # Colorize log lines for xterm.js display
            colored = "\n".join(_colorize_log_line(ln) for ln in text.split("\n"))
            log_chunks.append(colored)
            flush_buffer.append(colored)
            now = time.monotonic()
            if now - last_flush >= FLUSH_INTERVAL:
                _flush_log()
        proc.wait()
        _flush_log()  # final flush
    finally:
        timer.cancel()

    if timed_out.is_set():
        log = "".join(log_chunks) + f"\n\nTIMED OUT after {timeout}s"
        logger.warning("%s timed out after %ds", label, timeout)
        return log, False

    success = proc.returncode == 0
    log = "".join(log_chunks)
    logger.info("%s finished: returncode=%d", label, proc.returncode)
    return log, success


def _flush_log_text(job_id: str, text: str) -> None:
    """Send a chunk of log text to the server for live streaming."""
    try:
        post(
            f"/api/v1/jobs/{job_id}/log",
            JobLogAppend(lines=text).model_dump(),
            timeout=5,
        )
    except Exception:
        logger.debug("Log text flush failed for job %s", job_id, exc_info=True)


def _log_invocation(job_id: str, cmd: list[str]) -> None:
    """Log an esphome invocation to BOTH the Python logger and the user-visible
    job log stream.

    Bug reports are much easier to triage when the exact command line is in
    the log the user copy-pastes from the UI.
    """
    line = "Invoking: " + " ".join(cmd)
    logger.info(line)
    # Blue-ish ANSI so it stands out in the xterm viewer without looking alarming.
    _flush_log_text(job_id, f"\033[36m{line}\033[0m\n")


def _report_status(job_id: str, status_text: str) -> None:
    """Fire-and-forget status update to server."""
    try:
        post(
            f"/api/v1/jobs/{job_id}/status",
            JobStatusUpdate(status_text=status_text).model_dump(),
            timeout=5,
        )
    except Exception:
        logger.debug("Status update failed for job %s (%s)", job_id, status_text, exc_info=True)


def _submit_result(
    job_id: str,
    status: str,
    log: Optional[str],
    ota_result: Optional[str],
) -> None:
    """POST job result to server, retrying a few times on network errors."""
    # Build + validate the submission via the typed model. ``status`` is a
    # Literal["success","failed"] on the wire — pydantic will reject anything
    # else before it is ever sent. The cast + model_validate path makes mypy
    # happy without silencing the check with a blanket ignore.
    submission = JobResultSubmission.model_validate(
        {"status": status, "log": log, "ota_result": ota_result}
    )
    payload = submission.model_dump(exclude_none=True)

    for attempt in range(3):
        try:
            resp = post(f"/api/v1/jobs/{job_id}/result", payload, timeout=30)
            if resp.ok:
                logger.info("Submitted result for job %s: status=%s", job_id, status)
                return
            logger.warning(
                "Server rejected result for job %s: %d %s",
                job_id, resp.status_code, resp.text,
            )
            return
        except Exception as exc:
            logger.warning("Failed to submit result (attempt %d): %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2)


def _collect_firmware_variants(build_dir: str, target_stem: str) -> dict[str, Path]:
    """Find every compiled firmware binary under .esphome/build/<device>/.

    ESPHome layout after ``esphome compile`` is:
      {build_dir}/.esphome/build/{device_name}/.pioenvs/{device_name}/firmware.factory.bin   (ESP32)
      {build_dir}/.esphome/build/{device_name}/.pioenvs/{device_name}/firmware.bin           (ESP8266 or ESP32 OTA-only)

    Returns a mapping of ``variant → path``:
      - ``factory``: full flash image (ESP32 only; used for first
        USB/serial flash).
      - ``ota``: the ``firmware.bin`` shape (smaller, OTA-safe; ESP32
        produces this alongside factory; ESP8266 produces only this).

    The device name can differ from the target filename stem if the
    YAML uses substitutions, so we walk every device_dir under
    ``.esphome/build/``. For #69: returning *all* variants lets the
    server store them and the UI offer users a choice in the Download
    dropdown. Pre-#69 callers picked just one; that path is gone.
    """
    esphome_build = Path(build_dir) / ".esphome" / "build"
    if not esphome_build.is_dir():
        logger.warning(
            "Build tree %s does not exist — compile likely failed or produced no artifacts",
            esphome_build,
        )
        return {}

    variants: dict[str, Path] = {}
    for device_dir in esphome_build.iterdir():
        if not device_dir.is_dir():
            continue
        pioenvs = device_dir / ".pioenvs" / device_dir.name
        candidates = {
            "factory": pioenvs / "firmware.factory.bin",
            "ota": pioenvs / "firmware.bin",
        }
        for variant_name, path in candidates.items():
            if not path.is_file():
                continue
            # First-match wins when multiple device_dirs exist (usually
            # there's only one; substitutions don't spawn duplicates).
            if variant_name not in variants:
                variants[variant_name] = path
                logger.info(
                    "Located firmware variant %s for %s: %s (%d bytes)",
                    variant_name, target_stem, path, path.stat().st_size,
                )

    if not variants:
        logger.warning("No firmware binary found under %s", esphome_build)
    return variants


def _archive_firmware_to_server(
    job_id: str,
    build_dir: str,
    target_stem: str,
    *,
    client_id: Optional[str] = None,
    required: bool,
) -> bool:
    """Bug #9 (1.6.1): collect + upload every firmware variant to the server.

    Used by BOTH the download-only path (``required=True`` — the whole
    point of the job is the upload, so a failure fails the job) and the
    compile+OTA path (``required=False`` — the OTA is authoritative,
    the archive is a bonus). Returns True when at least one variant was
    uploaded successfully, False otherwise.

    Failures are surfaced into the job log via ``_flush_log_text`` so the
    user can diagnose from the Queue-tab Log modal. In best-effort mode
    the log line is a WARNING instead of an ERROR so it reads as a
    non-fatal archive failure rather than a real problem.
    """
    variants = _collect_firmware_variants(build_dir, target_stem)
    if not variants:
        tone = "\033[31mERROR" if required else "\033[33mWARNING"
        _flush_log_text(
            job_id,
            f"\n{tone}: Compile succeeded but no firmware binary was found "
            f"under .pioenvs/ — nothing to archive.\033[0m\n",
        )
        return False

    _report_status(job_id, "Archiving firmware")
    uploaded_any = False
    for variant_name, variant_path in variants.items():
        if _upload_firmware(
            job_id, variant_path,
            variant=variant_name, client_id=client_id,
        ):
            uploaded_any = True
        else:
            _flush_log_text(
                job_id,
                f"\n\033[33mWARNING: Failed to upload variant "
                f"{variant_name!r} — other variants (if any) will "
                f"continue.\033[0m\n",
            )
    if not uploaded_any:
        tone = "\033[31mERROR" if required else "\033[33mWARNING"
        _flush_log_text(
            job_id,
            f"\n{tone}: All firmware-variant uploads to server failed.\033[0m\n",
        )
        return False

    size_summary = ", ".join(
        f"{name}={variants[name].stat().st_size} B"
        for name in variants if name in variants
    )
    _flush_log_text(
        job_id,
        f"\nFirmware archived on server ({size_summary}). "
        "Download from the Queue or Compile-history panels.\n",
    )
    return True


def _upload_firmware(
    job_id: str,
    path: Path,
    *,
    variant: str = "factory",
    client_id: Optional[str] = None,
) -> bool:
    """POST one variant of the compiled binary to the server.

    Returns True on success. Failure reasons are surfaced into the
    job's log (via ``_flush_log_text``) so the user can diagnose from
    the Queue-tab Log modal without access to the worker's stdout.

    ``variant`` is an HTTP path segment — ``factory`` for ESP32's full
    flash image, ``ota`` for the smaller OTA-safe image. The server
    stores each under a separate filename so both are available for
    download (#69).
    """
    try:
        data = path.read_bytes()
    except Exception as exc:
        msg = f"Failed to read firmware {path}: {exc}"
        logger.error(msg)
        _flush_log_text(job_id, f"\n\033[31mUPLOAD ERROR: {msg}\033[0m\n")
        return False

    last_err = ""
    for attempt in range(3):
        try:
            resp = post_bytes(
                f"/api/v1/jobs/{job_id}/firmware/{variant}",
                data,
                timeout=600,
                client_id=client_id,
            )
            if resp.ok:
                logger.info(
                    "Uploaded firmware for job %s (variant=%s, %d bytes) → server",
                    job_id, variant, len(data),
                )
                return True
            last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            logger.warning(
                "Server rejected firmware for job %s: %s",
                job_id, last_err,
            )
            # Server rejections are deterministic — no retry.
            break
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Firmware upload attempt %d failed for job %s: %s",
                attempt + 1, job_id, last_err,
            )
            if attempt < 2:
                time.sleep(3)
    _flush_log_text(
        job_id,
        f"\n\033[31mUPLOAD ERROR: {last_err or 'unknown failure'}\033[0m\n",
    )
    return False


def _submit_ota_result(job_id: str, ota_result: str, ota_log: Optional[str]) -> None:
    """POST OTA result (and log) update to server."""
    for attempt in range(3):
        try:
            resp = post(
                f"/api/v1/jobs/{job_id}/result",
                {"status": "success", "ota_result": ota_result, "log": ota_log},
                timeout=30,
            )
            if resp.ok:
                logger.info("OTA result for job %s: %s", job_id, ota_result)
                return
        except Exception as exc:
            logger.warning("Failed to submit OTA result (attempt %d): %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2)


# ---------------------------------------------------------------------------
# Worker loop (one per parallel slot)
# ---------------------------------------------------------------------------

def worker_loop(
    worker_id: int,
    client_id: str,
    version_manager: VersionManager,
    stop_event: threading.Event,
) -> None:
    """Poll for jobs and execute them. Runs in its own thread."""
    _log_context.worker_id = worker_id
    _log_context.current_target = None
    logger.info("Worker %d started", worker_id)
    while not stop_event.is_set():
        # Pause polling when update / re-register / pending clean is set so
        # the main thread can reach idle state and handle the event.
        # Bug #4: ``_clean_pending`` lands here too — the heartbeat moved
        # the actual ``_clean_build_cache()`` to the main loop's idle
        # branch, but pollers must stop CLAIMING new jobs as soon as the
        # request arrives; otherwise we keep grabbing work and the
        # "drain to idle" never converges.
        if _reregister_needed.is_set() or _update_available.is_set() or _clean_pending.is_set():
            stop_event.wait(1)
            continue

        try:
            resp = requests.get(
                f"{SERVER_URL}/api/v1/jobs/next",
                headers={**HEADERS, "X-Client-Id": client_id, "X-Worker-Id": str(worker_id)},
                timeout=30,
            )
            _on_server_reachable()
            if resp.status_code == 401:
                _on_auth_failed()
                stop_event.wait(POLL_INTERVAL)
            elif resp.status_code == 204:
                _on_auth_ok()
                stop_event.wait(POLL_INTERVAL)
            elif resp.status_code == 200:
                _on_auth_ok()
                try:
                    assignment = JobAssignment.model_validate(resp.json())
                except ValidationError as exc:
                    logger.warning(
                        "Worker %d: malformed job assignment from server: %s",
                        worker_id, exc,
                    )
                    stop_event.wait(POLL_INTERVAL)
                    continue
                logger.info(
                    "Worker %d claimed job %s for target %s",
                    worker_id, assignment.job_id, assignment.target,
                )
                run_job(client_id, assignment.model_dump(), version_manager, worker_id)
                # No sleep after work — immediately poll for next job
            else:
                logger.warning(
                    "Worker %d: unexpected response from jobs/next: %d",
                    worker_id, resp.status_code,
                )
                stop_event.wait(POLL_INTERVAL)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            _on_server_unreachable(exc)
            stop_event.wait(POLL_INTERVAL)
        except Exception as exc:
            logger.exception("Worker %d: unexpected error in poll loop: %s", worker_id, exc)
            stop_event.wait(POLL_INTERVAL)

    logger.info("Worker %d stopped", worker_id)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def _initial_version_check(client_id: str) -> None:
    """Do one synchronous heartbeat immediately after registration.

    If the server has a newer worker version, sets _update_available so the
    main loop applies the update before picking up any jobs.
    """
    try:
        resp = post("/api/v1/workers/heartbeat", {
            "client_id": client_id,
            "system_info": collect_system_info(_ESPHOME_VERSIONS_DIR),
        }, timeout=10)
        if resp.ok:
            sv = resp.json().get("server_client_version")
            if sv and sv != CLIENT_VERSION:
                logger.info(
                    "Update available before first poll: local=%s server=%s",
                    CLIENT_VERSION, sv,
                )
                _update_available.set()
    except Exception as exc:
        logger.debug("Initial version check failed (non-fatal): %s", exc)


def _stop_workers(worker_stop: threading.Event, worker_threads: list[threading.Thread]) -> None:
    """Signal workers to stop and wait for them to finish their current jobs."""
    worker_stop.set()
    for t in worker_threads:
        t.join()


def _launch_workers(
    client_id: str,
    version_manager: VersionManager,
) -> tuple[threading.Event, list[threading.Thread]]:
    """Start MAX_PARALLEL_JOBS worker threads. Returns (stop_event, threads)."""
    stop = threading.Event()
    threads = []
    for i in range(MAX_PARALLEL_JOBS):
        t = threading.Thread(
            target=worker_loop,
            args=(i + 1, client_id, version_manager, stop),
            daemon=True,
            name=f"worker-{i + 1}",
        )
        t.start()
        threads.append(t)
    return stop, threads


def main() -> None:
    import signal  # noqa: PLC0415

    logger.info(
        "ESPHome Build Worker starting (hostname=%s, workers=%d)",
        HOSTNAME, MAX_PARALLEL_JOBS,
    )

    # Handle SIGTERM (sent by Docker on `docker stop`) — raise in main thread
    _shutdown_requested = threading.Event()

    def _sigterm_handler(signum, frame):
        logger.info("Received SIGTERM, shutting down")
        _shutdown_requested.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    version_manager = VersionManager(max_versions=MAX_ESPHOME_VERSIONS)

    # Pre-seed the requested ESPHome version so the first job runs immediately
    if ESPHOME_SEED_VERSION and not ESPHOME_BIN:
        logger.info("Pre-seeding ESPHome %s", ESPHOME_SEED_VERSION)
        try:
            version_manager.ensure_version(ESPHOME_SEED_VERSION)
            logger.info("ESPHome %s ready", ESPHOME_SEED_VERSION)
        except Exception as exc:
            logger.warning("Failed to pre-seed ESPHome %s: %s", ESPHOME_SEED_VERSION, exc)

    # DQ.9: startup sweep — prune orphan slot dirs (slot ids >= MAX_PARALLEL_JOBS,
    # left over from a higher-slot-count run) and run the byte-bound enforce.
    # The quota cell may still be None at this point (no heartbeat yet); in
    # that case enforce_quota is a no-op and the first heartbeat will trigger
    # a sweep if the server pushes a smaller quota than we have on disk.
    try:
        _run_disk_quota_sweep(label="startup", prune_orphans_first=True)
    except Exception:
        logger.exception("disk-quota: startup sweep failed")

    # Register with server
    client_id = register()

    # Check for available update before accepting any work
    _initial_version_check(client_id)

    # Start heartbeat + control-poll threads. Both share one stop event
    # so re-register / upgrade paths can tear them down together.
    def _start_bg_threads(cid: str) -> tuple[threading.Event, threading.Thread, threading.Thread]:
        ev = threading.Event()
        hb = threading.Thread(target=heartbeat_loop, args=(cid, ev), daemon=True, name="heartbeat")
        cp = threading.Thread(target=_control_poll_loop, args=(cid, ev), daemon=True, name="control-poll")
        hb.start()
        cp.start()
        return ev, hb, cp

    stop_heartbeat, hb_thread, cp_thread = _start_bg_threads(client_id)

    # Apply update immediately if detected (before starting workers)
    if _update_available.is_set():
        stop_heartbeat.set()
        hb_thread.join(timeout=2)
        cp_thread.join(timeout=2)
        _apply_update(client_id)  # may os.execv — never returns on success
        # Update failed — restart both
        stop_heartbeat, hb_thread, cp_thread = _start_bg_threads(client_id)

    logger.info("Starting %d worker(s), polling every %ds", MAX_PARALLEL_JOBS, POLL_INTERVAL)
    worker_stop, worker_threads = _launch_workers(client_id, version_manager)

    try:
        while not _shutdown_requested.is_set():
            # Re-register if the heartbeat told us the server doesn't know us.
            # Wait until all workers are idle so in-flight jobs can complete.
            if _reregister_needed.is_set() and _is_idle():
                _reregister_needed.clear()
                _stop_workers(worker_stop, worker_threads)
                stop_heartbeat.set()
                hb_thread.join(timeout=2)
                cp_thread.join(timeout=2)

                client_id = register()

                stop_heartbeat, hb_thread, cp_thread = _start_bg_threads(client_id)
                worker_stop, worker_threads = _launch_workers(client_id, version_manager)

            # Apply pending update only when all workers are idle
            elif _update_available.is_set() and _is_idle():
                _stop_workers(worker_stop, worker_threads)
                stop_heartbeat.set()
                hb_thread.join(timeout=2)
                cp_thread.join(timeout=2)
                _apply_update(client_id)  # may os.execv — never returns on success
                # Update failed — restart heartbeat + control-poll and workers
                stop_heartbeat, hb_thread, cp_thread = _start_bg_threads(client_id)
                worker_stop, worker_threads = _launch_workers(client_id, version_manager)

            # Bug #4: run the deferred build-cache clean once all workers
            # have drained. The poll loops are paused while ``_clean_pending``
            # is set so we'll reach idle as soon as in-flight jobs finish;
            # then the clean runs on a quiet worker and pollers resume.
            elif _clean_pending.is_set() and _is_idle():
                logger.info("Worker is idle — running deferred build-cache clean")
                _clean_build_cache()
                _clean_pending.clear()

            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl-C)")
    finally:
        worker_stop.set()
        stop_heartbeat.set()
        hb_thread.join(timeout=2)
        cp_thread.join(timeout=2)
        deregister(client_id)


if __name__ == "__main__":
    main()
