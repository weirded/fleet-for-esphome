"""System information gathering — psutil for memory/CPU/disk, stdlib for the rest.

Importable standalone; no dependency on other client modules.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from typing import Optional

import psutil

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Captured at process start so uptime can be computed on each heartbeat.
_PROCESS_START_TIME: float = time.monotonic()


def _benchmark_cpu() -> int:
    """Run a quick CPU benchmark. Returns a relative performance score (SHA256 ops/sec / 1000)."""
    import hashlib  # noqa: PLC0415
    data = b"benchmark" * 1000
    count = 0
    deadline = time.monotonic() + 1.0  # run for 1 second
    while time.monotonic() < deadline:
        hashlib.sha256(data).digest()
        count += 1
    return count


# Computed once at startup; included in every heartbeat as a relative CPU score.
_CPU_PERF_SCORE: int = _benchmark_cpu()

# Prime psutil.cpu_percent() — the first non-blocking call always returns 0.0
# because it has nothing to diff against. A throwaway call at startup gives
# later calls a meaningful baseline.
psutil.cpu_percent(interval=None)


# ---------------------------------------------------------------------------
# OS / hardware detection — psutil doesn't provide distro names or CPU models,
# so these stay stdlib.
# ---------------------------------------------------------------------------

def _get_os_version() -> str:
    """Return a human-readable OS version string using only stdlib."""
    system = platform.system()

    if system == "Darwin":
        mac_ver = platform.mac_ver()[0]
        return f"macOS {mac_ver}" if mac_ver else "macOS"

    if system == "Linux":
        # Parse /etc/os-release for NAME and VERSION_ID (most distros)
        os_release: dict[str, str] = {}
        try:
            with open("/etc/os-release", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        key, _, val = line.partition("=")
                        os_release[key.strip()] = val.strip().strip('"')
        except OSError:
            pass

        name = os_release.get("NAME") or os_release.get("ID", "")
        version = os_release.get("VERSION_ID", "")
        if name and version:
            return f"{name} {version}"
        if name:
            return name

        # Fallback for minimal containers without /etc/os-release
        kernel = platform.release()
        return f"Linux {kernel}" if kernel else "Linux"

    # Windows or other
    return platform.platform()


def _get_cpu_model() -> str:
    """Return CPU model string.

    psutil doesn't expose the CPU brand string, so fall back to /proc/cpuinfo
    on Linux, sysctl on macOS, and platform.processor()/machine() elsewhere.
    """
    system = platform.system()

    if system == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=3,
            )
            model = result.stdout.strip()
            if model:
                return model
        except Exception:
            pass
        # Apple Silicon reports via hw.model (e.g. "Apple M1 Pro")
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.model"],
                capture_output=True, text=True, timeout=3,
            )
            model = result.stdout.strip()
            if model:
                return model
        except Exception:
            pass

    if system == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if ":" in line:
                        key, _, val = line.partition(":")
                        key = key.strip().lower()
                        if key in ("model name", "hardware", "cpu model"):
                            val = val.strip()
                            if val:
                                return val
        except OSError:
            pass

    machine = platform.machine()
    processor = platform.processor()
    return processor or machine or "Unknown"


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _format_memory(bytes_: int) -> str:
    """Return a human-readable memory string, e.g. '16 GB' or '512 MB'."""
    gb = bytes_ / (1024 ** 3)
    if gb >= 1:
        return f"{round(gb)} GB"
    mb = bytes_ / (1024 ** 2)
    return f"{round(mb)} MB"


def _format_uptime(seconds: float) -> str:
    """Return uptime as a compact human-readable string, e.g. '2d 3h' or '45m'."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_system_info(versions_dir: str = "/esphome-versions") -> dict:
    """Gather hardware/OS details. All fields are best-effort.

    *versions_dir* is used to report disk space on the build volume.  Pass the
    ``ESPHOME_VERSIONS_DIR`` env value from the caller so this module stays
    decoupled from client.py.

    When running in Docker on a non-Linux host, the container sees the VM's
    Linux.  Set ``HOST_PLATFORM`` to override ``os_version`` with the actual
    host OS (e.g. ``macOS 15.3 (Apple M1 Pro)``).
    """
    os_version = os.environ.get("HOST_PLATFORM") or _get_os_version()

    # Memory — psutil.virtual_memory() handles Linux (/proc/meminfo), macOS
    # (sysctl), Windows, and FreeBSD in one call.
    total_memory: Optional[str] = None
    try:
        total_memory = _format_memory(psutil.virtual_memory().total)
    except Exception:
        pass

    # CPU utilization — psutil.cpu_percent(interval=None) compares against the
    # previous call. It was primed at module load, so heartbeats get accurate
    # utilization percentages (not just load-average approximations).
    cpu_usage: Optional[float] = None
    try:
        cpu_usage = round(psutil.cpu_percent(interval=None), 1)
    except Exception:
        pass

    # Disk space on the build volume — psutil.disk_usage() is cross-platform.
    disk_total: Optional[str] = None
    disk_free: Optional[str] = None
    disk_pct: Optional[int] = None
    disk_free_bytes: Optional[int] = None
    try:
        du = psutil.disk_usage(versions_dir)
        disk_total = _format_memory(du.total)
        disk_free = _format_memory(du.free)
        disk_pct = round(du.percent)
        disk_free_bytes = du.free
    except Exception:
        pass

    # 5.2: build cache stats. After #45 the layout is:
    #   cache/<stem>/   shared per-target cache (.pio/ + .esphome/)
    #   slots/<slot>/<stem>/  per-slot working dirs
    # Count distinct targets via cache/ entries; sum sizes from BOTH dirs
    # so the user sees the on-disk footprint they'd reclaim by cleaning.
    cached_targets = 0
    cache_size_mb: Optional[int] = None
    try:
        cache_dir = os.path.join(versions_dir, "cache")
        slots_dir = os.path.join(versions_dir, "slots")
        if os.path.isdir(cache_dir):
            cached_targets = sum(
                1 for entry in os.listdir(cache_dir)
                if os.path.isdir(os.path.join(cache_dir, entry))
            )
        total_bytes = 0
        for root_dir in (cache_dir, slots_dir):
            if not os.path.isdir(root_dir):
                continue
            for dirpath, _dirnames, filenames in os.walk(root_dir):
                for f in filenames:
                    try:
                        total_bytes += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
        cache_size_mb = round(total_bytes / (1024 * 1024))
    except Exception:
        pass

    return {
        "cpu_arch": platform.machine(),
        "os_version": os_version,
        "cpu_cores": psutil.cpu_count(logical=True) or os.cpu_count(),
        "cpu_model": _get_cpu_model(),
        "total_memory": total_memory,
        "uptime": _format_uptime(time.monotonic() - _PROCESS_START_TIME),
        "perf_score": _CPU_PERF_SCORE,
        "cpu_usage": cpu_usage,
        "disk_total": disk_total,
        "disk_free": disk_free,
        "disk_used_pct": disk_pct,
        "disk_free_bytes": disk_free_bytes,
        "cached_targets": cached_targets,
        "cache_size_mb": cache_size_mb,
    }
