"""ESPHome version manager with LRU eviction."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Iterator

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


def _install_timeout_from_env() -> int:
    """Seconds to allow one ``pip install esphome==X`` attempt (#193).

    The 300 s default is sized for "slow ARM host" (see
    ``VersionManager._PIP_INSTALL_TIMEOUT``), but slow has no upper bound:
    the reporter of #193 measured ~14 minutes for a full ESPHome install on
    a Zimaboard, where every attempt hit the wall and the version could
    never finish installing at all. There is nothing the user can do about
    that from the UI, so make the ceiling an env var like every other
    worker knob (``MIN_FREE_DISK_PCT``, ``JOB_TIMEOUT``, …).

    Floor of 60 s: below that even a warm-cache install on a fast host
    starts flaking, and a too-low value fails in a way that looks like a
    network problem rather than a misconfiguration. A bad value warns and
    falls back rather than crashing the worker at import time — this runs
    before any of the worker's own error handling exists.
    """
    raw = os.environ.get("ESPHOME_INSTALL_TIMEOUT")
    if raw is None or not raw.strip():
        return 300
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ESPHOME_INSTALL_TIMEOUT=%r is not an integer — using 300s", raw,
        )
        return 300
    if value < 60:
        logger.warning(
            "ESPHOME_INSTALL_TIMEOUT=%ds is below the 60s floor — using 60s", value,
        )
        return 60
    return value


ESPHOME_INSTALL_TIMEOUT = _install_timeout_from_env()


# pip reports a Python-floor mismatch in two places, and neither one says
# "your image is too old" — which is what the user actually needs to hear.
_PY_FLOOR_MARKER = "require a different python version"
_NO_DIST_MARKER = "no matching distribution found"


def diagnose_pip_failure(version: str, stdout: str, stderr: str) -> str | None:
    """Turn a pip install failure into one actionable sentence, or None (#240).

    #240 was reported as "Latest version not available": the UI offered
    ESPHome 2026.7.3, every job pinned to it died, and the log was a
    200-version wall ending in ``No matching distribution found for
    esphome==2026.7.3``. Read literally that says the release doesn't exist.
    It does — pip had silently filtered it out because ESPHome 2026.7 raised
    its Python floor to 3.12 and the shipped worker image was still on 3.11,
    so every 2026.7.x candidate was excluded before the version match ran
    and the list simply stopped at 2026.6.5.

    The underlying incompatibility was fixed in 1.7.2 (images moved to
    Python 3.13), but the *message* is what made it take a round-trip to
    diagnose, and the failure mode recurs on its own schedule: ESPHome will
    raise its floor again, and a worker running a stale image will hit this
    exact wall each time. Name the cause and the fix instead of pasting the
    version list. Same reasoning as #114's bind-conflict message.

    Returns None for ordinary failures (typo'd version, unreachable index),
    which keep the existing generic error — a wrong guess here would be
    worse than no guess.
    """
    blob = f"{stdout}\n{stderr}".lower()
    if _NO_DIST_MARKER not in blob:
        return None
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if _PY_FLOOR_MARKER in blob:
        return (
            f"ESPHome {version} cannot be installed: it requires a newer "
            f"Python than this worker's {running}. pip excluded every "
            f"matching release before looking at the version number, which "
            f"is why the log says 'no matching distribution' for a release "
            f"that does exist. Update the worker's Docker image (or the "
            f"add-on, for the built-in worker) — the Python version is "
            f"baked into the image and cannot be changed at runtime. "
            f"Pinning an older ESPHome version is the workaround (#240)."
        )
    return (
        f"ESPHome {version} was not found on the package index. Check the "
        f"version exists on PyPI and that this worker can reach the index "
        f"(running Python {running}); if the release is recent, an outdated "
        f"worker image whose Python is below ESPHome's floor produces this "
        f"same message (#240)."
    )

# #119 (round 2): the in-container local worker shares
# ``/data/esphome-versions/`` with the server's lazy-installed bundling
# venv (see ``main.py``'s ``ESPHOME_VERSIONS_DIR=/data/esphome-versions``
# in the local-worker spawn). The worker never *runs a job* on the
# server's selected version, so from the worker's LRU / disk-quota point
# of view that venv looks idle and gets evicted first — which deletes the
# exact venv ``scanner.create_bundle`` shells into. Every subsequent
# bundle then fails with ``FileNotFoundError: '.../bin/python'`` until the
# add-on restarts (the original #119 symptom; that fix only covered the
# Clean-Cache path, not LRU / disk-quota eviction).
#
# The server publishes its active version to this sentinel file in the
# shared dir; every worker eviction path treats those versions as pinned.
# Remote workers (own dir, no sentinel) read an empty set — no change.
SERVER_ACTIVE_VERSION_FILE = ".server-active-version"


def read_server_active_versions(base: Path) -> set[str]:
    """Versions the server has pinned as its active bundling venv(s).

    Returns an empty set when the sentinel is absent/unreadable (the
    common case for remote workers with their own versions dir).
    """
    try:
        raw = (Path(base) / SERVER_ACTIVE_VERSION_FILE).read_text()
    except OSError:
        return set()
    return {line.strip() for line in raw.splitlines() if line.strip()}


def write_server_active_version(base: Path, version: str) -> None:
    """Publish *version* as the server's active bundling venv.

    Best-effort: a write failure just means the worker may evict the
    venv and the server self-heals by reinstalling (see
    ``scanner.create_bundle``). Never raises.
    """
    try:
        base = Path(base)
        base.mkdir(parents=True, exist_ok=True)
        (base / SERVER_ACTIVE_VERSION_FILE).write_text(version + "\n")
    except OSError:
        logger.warning(
            "Could not write %s sentinel under %s; local worker may evict "
            "the server's bundling venv (server will self-heal)",
            SERVER_ACTIVE_VERSION_FILE, base, exc_info=True,
        )


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
        """True if *version*'s venv exists **and** was built by this interpreter.

        #243: checking only that ``bin/esphome`` exists reports a venv built
        by a *different* Python as installed. That happens in normal
        operation — the add-on image's Python moves (3.11 → 3.13 in 1.7.2 for
        ESPHome 2026.7's floor), or a rollback/stale-tag deploy puts an older
        image on top of a `/data` a newer one wrote. The venv is then
        unusable: ``scanner._activate_esphome_venv`` looks for
        ``lib/python{running}/site-packages``, finds only the other version's,
        logs "venv site-packages missing", and the UI says *"ESPHome X install
        failed"* — with no reinstall ever attempted, because this function
        kept insisting it was installed.

        Treating a mismatch as not-installed lets the existing wipe-and-
        rebuild path in ``_install`` self-heal it, which is the same theme as
        the partial-venv recovery in PR #251.
        """
        if not self._esphome_bin(version).exists():
            return False
        return self._venv_matches_running_python(version)

    def _venv_matches_running_python(self, version: str) -> bool:
        """True if *version*'s venv holds a ``lib/python{M.N}/`` for us.

        ``pyvenv.cfg`` also carries a ``version`` key, but it records the
        *full* patch version of the interpreter that created the venv
        (``3.13.1``), while what actually has to match is the ``M.N`` in the
        ``site-packages`` path — a 3.13.1 → 3.13.2 base-image bump keeps the
        same ``lib/python3.13/`` and must not force a needless rebuild. So
        check the directory that gets imported from, not the cfg string.

        Unreadable/absent ``lib/`` is treated as a mismatch: a venv we can't
        verify is one we should rebuild rather than trust.
        """
        want = f"python{sys.version_info.major}.{sys.version_info.minor}"
        lib_dir = self._venv_path(version) / "lib"
        try:
            names = [d.name for d in lib_dir.iterdir() if d.is_dir()]
        except OSError:
            logger.warning(
                "ESPHome %s: cannot read %s to verify the venv interpreter — "
                "treating as not installed so it gets rebuilt", version, lib_dir,
            )
            return False
        if want in names:
            return True
        logger.warning(
            "ESPHome %s venv was built by a different Python (found %s, this "
            "image runs %s) — rebuilding. This is expected after an add-on "
            "image update that changed the Python version (#243).",
            version, ", ".join(sorted(names)) or "nothing", want,
        )
        return False

    def _evict_lru(self, keep_version: str | None = None) -> bool:
        """Remove the least-recently-used version from disk and LRU cache.

        Must be called with self._lock held.
        Skips *keep_version* if provided (the version about to be installed).
        Skips versions the server has pinned as its active bundling venv
        (#119 round 2 — never evict the shared dir's server venv).
        Returns True if a version was evicted, False if nothing evictable.
        """
        protected = read_server_active_versions(self._base)
        for version, path in self._lru.items():
            if version == keep_version or version in protected:
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

    # Generous timeout for pip install on slow ARM hosts (HAOS, Raspberry Pi).
    # PyPI downloads + sdist compiles (some ESPHome transitive deps lack ARM
    # wheels and must be compiled from source) can legitimately take 3–5 min.
    # Bug #127: default (None / unbounded) caused "uv installation via pip
    # timed out" reports on HAOS 2026.4.4 — pip's own socket-level timeout is
    # separate from this subprocess timeout, so this bounds the whole install.
    # #193: raise it with ``ESPHOME_INSTALL_TIMEOUT`` when 300 s isn't enough
    # (slow single-board hosts have been measured at ~14 min).
    _PIP_INSTALL_TIMEOUT = ESPHOME_INSTALL_TIMEOUT  # seconds

    def _install(self, version: str) -> None:
        """Create a venv and install esphome==version into it.

        Must NOT be called with self._lock held (long-running subprocess).
        Retries once on timeout or network failure (#127).
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

        # Log the effective PyPI index so corporate-proxy / offline failures
        # are debuggable from the log alone (#127).
        index_url = os.environ.get("PIP_INDEX_URL", "https://pypi.org/simple/")

        last_exc: Exception | None = None
        for attempt in range(1, 3):  # attempts 1 and 2
            logger.info(
                "Running: %s  (index=%s, timeout=%ds, attempt=%d/2)",
                " ".join(install_cmd), index_url, self._PIP_INSTALL_TIMEOUT, attempt,
            )
            try:
                result = subprocess.run(
                    install_cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._PIP_INSTALL_TIMEOUT,
                )
            except subprocess.TimeoutExpired as exc:
                last_exc = exc
                logger.warning(
                    "pip install esphome==%s timed out after %ds (attempt %d/2)",
                    version, self._PIP_INSTALL_TIMEOUT, attempt,
                )
                if attempt < 2:
                    logger.warning(
                        "pip install retry 1/1 after %s: %s", type(exc).__name__, exc,
                    )
                continue

            if result.returncode == 0:
                logger.info("esphome==%s installed successfully", version)
                return

            stderr_excerpt = (result.stderr or "")[-2000:]  # last 2000 chars
            stdout_excerpt = (result.stdout or "")[-1000:]
            logger.error(
                "pip install esphome==%s failed (exit %d, attempt %d/2):\nstderr: %s\nstdout: %s",
                version, result.returncode, attempt, stderr_excerpt, stdout_excerpt,
            )
            # Non-zero exit is a hard failure (bad version, bad index, etc.) —
            # don't retry, it won't help.
            shutil.rmtree(str(venv_dir), ignore_errors=True)
            diagnosis = diagnose_pip_failure(
                version, result.stdout or "", result.stderr or "",
            )
            if diagnosis:
                logger.error("%s", diagnosis)
                raise RuntimeError(f"{diagnosis}\n\n{stderr_excerpt}")
            raise RuntimeError(
                f"pip install esphome=={version} failed (exit {result.returncode}):\n"
                f"{stderr_excerpt}"
            )

        # Reached only after two consecutive timeouts.
        shutil.rmtree(str(venv_dir), ignore_errors=True)
        raise RuntimeError(
            f"pip install esphome=={version} timed out after {self._PIP_INSTALL_TIMEOUT}s "
            f"(network slow or PyPI unreachable). "
            f"Retry the compile or check the worker host's network."
        ) from last_exc

    @contextlib.contextmanager
    def _install_file_lock(self, version: str) -> Iterator[None]:
        """Cross-process exclusive lock around installing *version* (#317).

        ``self._installing`` only dedupes threads sharing one
        VersionManager. The add-on runs several: the server builds a fresh
        one per ``ensure_esphome_installed`` call (UI version change + HA
        add-on version poller fired 8 s apart on an ESPHome bump), and the
        in-container local worker has its own in another process — all
        on the same ``versions_base``. Unserialised, the second install's
        "remove stale venv" wiped the first one mid-pip (``certifi/cacert.pem``
        missing, ``ensurepip`` failing) and the UI showed "install failed".

        ``flock`` conflicts between separate ``open()`` calls even within
        one process, so this covers both cases. Lock files persist (handles
        only). Failure to lock degrades to the old unserialised behaviour.
        """
        lock_path = self._base / f".install-{version}.lock"
        fp = None
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            fp = open(lock_path, "w", encoding="utf-8")
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            logger.warning(
                "Could not acquire install lock %s (%s) — proceeding unserialised",
                lock_path, exc,
            )
            if fp is not None:
                fp.close()
                fp = None
        try:
            yield
        finally:
            if fp is not None:
                try:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
                finally:
                    fp.close()

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
                        # _evict_lru returns False once only protected /
                        # keep versions remain — stop then so we don't spin
                        # forever (the server's pinned venv legitimately
                        # keeps us above max_versions).
                        if not self._evict_lru(keep_version=version):
                            break
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
                with self._install_file_lock(version):
                    # Another process (or VersionManager) may have finished
                    # this install while we waited on the lock (#317).
                    if self._is_installed(version):
                        logger.info(
                            "esphome==%s was installed concurrently by another "
                            "process — reusing it", version,
                        )
                    else:
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
