"""ESPHome version manager with LRU eviction."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

VERSIONS_BASE = Path(os.environ.get("ESPHOME_VERSIONS_DIR", "/esphome-versions"))
# DQ.8: the disk-quota engine (``disk_quota.py``) is the authoritative
# bound on cache size now. We always keep exactly 1 venv (most recently
# used); byte-bounded eviction across every category (caches, slots,
# pio-slots) happens in ``client.py`` at job boundaries via
# ``disk_quota.enforce_quota``. The ``MAX_ESPHOME_VERSIONS`` env var
# becomes a no-op with a one-time warning if set to anything but 1,
# kept readable for backwards compat with deployed worker docker
# invocations.
MAX_ESPHOME_VERSIONS = 1
_LEGACY_MAX_ESPHOME_VERSIONS = os.environ.get("MAX_ESPHOME_VERSIONS")
if (
    _LEGACY_MAX_ESPHOME_VERSIONS is not None
    and _LEGACY_MAX_ESPHOME_VERSIONS.strip() not in ("", "1")
):
    logger.warning(
        "MAX_ESPHOME_VERSIONS=%s is ignored — the disk-quota engine "
        "now bounds the cache by bytes, not by venv count. Always 1 "
        "venv kept (the most recently used).",
        _LEGACY_MAX_ESPHOME_VERSIONS,
    )
# Minimum free disk percentage before we start evicting versions
MIN_FREE_DISK_PCT = int(os.environ.get("MIN_FREE_DISK_PCT", "10"))


class VersionManager:
    """
    Manages multiple ESPHome virtualenv installations.

    Each version lives in ``{VERSIONS_BASE}/{version}/``.
    An LRU cache evicts the oldest version when the count would
    exceed ``max_versions``.

    Thread-safe: multiple workers may call ensure_version() concurrently.
    Two workers requesting the same version share a single install run.
    """

    def __init__(
        self,
        versions_base: Path = VERSIONS_BASE,
        max_versions: int = MAX_ESPHOME_VERSIONS,
    ) -> None:
        self._base = versions_base
        self._max_versions = max_versions
        # OrderedDict[version_str, Path]: most-recent at end
        self._lru: OrderedDict[str, Path] = OrderedDict()
        self._lock = threading.Lock()
        # Per-version Events for in-progress installs; signals waiters when done
        self._installing: dict[str, threading.Event] = {}
        self._base.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_existing(self) -> None:
        """Scan disk for already-installed versions and load them into LRU."""
        for entry in sorted(self._base.iterdir(), key=lambda p: p.stat().st_mtime):
            if entry.is_dir() and (entry / "bin" / "esphome").exists():
                self._lru[entry.name] = entry
        logger.info(
            "Found %d existing ESPHome versions: %s",
            len(self._lru),
            list(self._lru.keys()),
        )

    def _venv_path(self, version: str) -> Path:
        return self._base / version

    def _esphome_bin(self, version: str) -> Path:
        return self._venv_path(version) / "bin" / "esphome"

    def _is_installed(self, version: str) -> bool:
        return self._esphome_bin(version).exists()

    def _evict_lru(self, keep_version: str | None = None) -> bool:
        """Remove the least-recently-used version from disk and LRU cache.

        Must be called with self._lock held.
        Skips *keep_version* if provided (the version about to be installed).
        Returns True if a version was evicted, False if nothing to evict.
        """
        for version, path in self._lru.items():
            if version == keep_version:
                continue
            logger.info("Evicting ESPHome version %s from %s", version, path)
            try:
                shutil.rmtree(str(path), ignore_errors=True)
            except Exception:
                logger.exception("Failed to remove version dir %s", path)
            del self._lru[version]
            return True
        return False

    def _free_disk_pct(self) -> float | None:
        """Return free disk percentage on the versions volume, or None on error."""
        try:
            st = os.statvfs(str(self._base))
            total = st.f_frsize * st.f_blocks
            free = st.f_frsize * st.f_bavail
            return (free / total) * 100 if total > 0 else None
        except Exception:
            return None

    def _ensure_disk_space(self, keep_version: str | None = None) -> None:
        """Evict LRU versions until free disk exceeds MIN_FREE_DISK_PCT.

        Must be called with self._lock held.
        """
        while len(self._lru) > 1:  # always keep at least the current version
            pct = self._free_disk_pct()
            if pct is None or pct >= MIN_FREE_DISK_PCT:
                break
            logger.warning(
                "Disk free %.1f%% < %d%% threshold — evicting unused ESPHome version",
                pct, MIN_FREE_DISK_PCT,
            )
            if not self._evict_lru(keep_version=keep_version):
                break

    def _install(self, version: str) -> None:
        """Create a venv and install esphome==version into it.

        Must NOT be called with self._lock held (long-running subprocess).
        """
        venv_dir = self._venv_path(version)
        logger.info("Installing esphome==%s into %s", version, venv_dir)

        # Wipe any stale/partial venv from a previous failed attempt so we
        # start clean (otherwise a venv missing bin/pip causes FileNotFoundError
        # on every subsequent restart until /data is cleared).
        if venv_dir.exists():
            logger.info("Removing stale venv at %s before reinstall", venv_dir)
            shutil.rmtree(str(venv_dir), ignore_errors=True)

        venv_cmd = [sys.executable, "-m", "venv", str(venv_dir)]
        logger.info("Running: %s", " ".join(venv_cmd))
        try:
            subprocess.run(
                venv_cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            logger.error(
                "python -m venv failed (exit %d):\nstderr: %s\nstdout: %s",
                exc.returncode, stderr, stdout,
            )
            shutil.rmtree(str(venv_dir), ignore_errors=True)
            raise

        pip = venv_dir / "bin" / "pip"
        if not pip.exists():
            shutil.rmtree(str(venv_dir), ignore_errors=True)
            raise RuntimeError(
                f"venv created at {venv_dir} but bin/pip is missing — "
                "ensurepip may be unavailable in this Python installation"
            )

        install_cmd: list[str] = [
            str(pip), "install", "--no-cache-dir", f"esphome=={version}",
        ]

        logger.info("Running: %s", " ".join(install_cmd))
        result = subprocess.run(
            install_cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr_excerpt = (result.stderr or "")[-2000:]  # last 2000 chars
            stdout_excerpt = (result.stdout or "")[-1000:]
            logger.error(
                "pip install esphome==%s failed (exit %d):\nstderr: %s\nstdout: %s",
                version, result.returncode, stderr_excerpt, stdout_excerpt,
            )
            shutil.rmtree(str(venv_dir), ignore_errors=True)
            raise RuntimeError(
                f"pip install esphome=={version} failed (exit {result.returncode}):\n"
                f"{stderr_excerpt}"
            )

        logger.info("esphome==%s installed successfully", version)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_version(self, version: str) -> str:
        """
        Ensure ESPHome *version* is installed.

        Returns the path to the ``esphome`` binary.
        Installs if necessary; evicts LRU version if limit would be exceeded.
        Thread-safe: concurrent calls for the same version share one install.
        """
        while True:
            install_event: threading.Event | None = None
            wait_event: threading.Event | None = None

            with self._lock:
                if self._is_installed(version):
                    if version in self._lru:
                        self._lru.move_to_end(version)
                    else:
                        self._lru[version] = self._venv_path(version)
                    logger.debug("esphome==%s already installed", version)
                    return str(self._esphome_bin(version))

                if version in self._installing:
                    # Another thread is installing this version — wait for it
                    wait_event = self._installing[version]
                else:
                    # We'll do the install; evict if at capacity
                    while len(self._lru) >= self._max_versions:
                        self._evict_lru(keep_version=version)
                    # Also evict if disk is low
                    self._ensure_disk_space(keep_version=version)
                    install_event = threading.Event()
                    self._installing[version] = install_event

            if wait_event is not None:
                logger.debug("Waiting for esphome==%s install in progress...", version)
                if not wait_event.wait(timeout=600):  # 10 minute timeout
                    logger.error("Timed out waiting for esphome==%s install", version)
                    raise RuntimeError(f"Timed out waiting for esphome=={version} install (another thread may have crashed)")
                continue  # re-check from the top

            # We own the install — run outside the lock (slow subprocess)
            assert install_event is not None
            try:
                self._install(version)
                with self._lock:
                    self._lru[version] = self._venv_path(version)
                    self._installing.pop(version, None)
            except Exception:
                with self._lock:
                    self._installing.pop(version, None)
                install_event.set()  # wake up any waiters
                raise

            install_event.set()  # wake up waiters
            return str(self._esphome_bin(version))

    def get_esphome_path(self, version: str) -> str:
        """Return the path to the esphome binary for *version* (must be installed)."""
        path = self._esphome_bin(version)
        if not path.exists():
            raise FileNotFoundError(
                f"esphome=={version} is not installed at {path}. "
                "Call ensure_version() first."
            )
        with self._lock:
            if version in self._lru:
                self._lru.move_to_end(version)
        return str(path)

    def installed_versions(self) -> list[str]:
        """Return list of installed versions (LRU order, oldest first)."""
        with self._lock:
            return list(self._lru.keys())
