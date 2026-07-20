"""In-app user-facing settings, persisted to ``/data/settings.json``.

Scope: product feature toggles and operational knobs that we expect
users to edit at runtime — auto-commit-on-save, job-history retention,
cache budgets. Deliberately separate from :mod:`app_config` (Supervisor's
``options.json``) because:

1. Supervisor's Configuration tab triggers a full add-on restart on every
   edit — hostile UX for day-to-day toggles.
2. Product settings shouldn't clutter the deployment-plumbing surface
   (token, port, ``require_ha_auth``) that Supervisor *does* own.

See ``dev-plans/WORKITEMS-1.6.md`` §Settings for the full rationale.

Contract:

- :func:`get_settings` returns the current in-memory singleton. Cheap,
  safe to call from any code path. Consumers MUST call it at decision
  time (not at startup) so PATCH propagates without a restart.
- :func:`update_settings` validates + persists atomically + rotates the
  singleton. Call sites are expected to be async (it takes the lock).
- :func:`init_settings` runs once at server startup — loads the file or
  seeds it from ``options.json`` (one-time import).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path("/data/settings.json")
OPTIONS_FILE = Path("/data/options.json")

# Fields migrated out of options.json. On first boot where settings.json
# is absent, their values are seeded from options.json if present. After
# that, settings.json is the only source of truth — edits in Supervisor
# Configuration have no effect and are documented in DOCS.md.
#
# Note: the `token` in options.json maps to ``server_token`` here — the
# field got renamed during the move to make it obviously distinct from
# any per-worker / per-user token in the codebase.
IMPORT_FROM_OPTIONS: tuple[str, ...] = (
    "job_history_retention_days",
    "firmware_cache_max_gb",
    "firmware_retention_days",
    "job_log_retention_days",
    "job_timeout",
    "ota_timeout",
    "worker_offline_threshold",
    "device_poll_interval",
    "require_ha_auth",
)

# Legacy token file, written by pre-1.6 releases when no explicit token
# was set in options.json. We honour it once at import time so existing
# installs don't suddenly issue a fresh token on upgrade.
LEGACY_TOKEN_FILE = Path("/data/auth_token")

# Bug #9: marker file that records we've already cleared Supervisor's
# stale options cache. When this file is present, we don't call the
# Supervisor API again on subsequent boots. Persisted under /data so it
# survives add-on restarts but is discarded if the user wipes /data.
SUPERVISOR_OPTIONS_CLEARED_MARKER = Path("/data/.supervisor_options_cleared")

# Supervisor HTTP endpoint that returns this add-on's user-stored
# options. Critical for the SP.8 migration path: when config.yaml
# drops its ``options:``/``schema:`` blocks, Supervisor stops
# projecting user options into /data/options.json (leaving it as
# ``{}``) — but the user's values are still stored in Supervisor's
# own state. We query this endpoint so first-boot imports still see
# the full pre-strip option set and don't rotate tokens on upgrade.
SUPERVISOR_INFO_URL = "http://supervisor/addons/self/info"


class SettingsValidationError(ValueError):
    """Raised by :func:`update_settings` when a value fails validation.

    Carries the offending field name so the REST layer can return a 400
    that pinpoints the problem.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass
class AppSettings:
    """User-facing settings editable at runtime via ``/ui/api/settings``."""

    # #97 + #98: top-level tristate for the AV.* config-versioning
    # feature set. When not ``'on'``, the server skips all git
    # operations (no init, no auto-commit, no rollback, no history
    # fetch) and the UI dims the sub-settings.
    #   - ``'on'``    — versioning active; git wrappers run.
    #   - ``'off'``   — versioning explicitly off; the module is inert.
    #   - ``'unset'`` — the user hasn't decided yet. Treated like
    #                   ``'off'`` on the server side (no git ops) so a
    #                   fresh install doesn't mutate the config
    #                   directory before the user consents; the UI
    #                   shows a one-time onboarding modal that prompts
    #                   Pat to pick ``'on'`` or ``'off'``. #99-era
    #                   upgrades with a prior ``versioning_enabled:
    #                   true`` boolean are migrated to ``'on'`` at
    #                   settings-load time (see ``_migrate_settings``).
    versioning_enabled: str = "unset"
    auto_commit_on_save: bool = True
    # Author used on Fleet-originated auto-commits (AV.2). Only applied
    # when the repo itself has no ``user.name``/``user.email`` configured
    # at any level (repo-local, global, system) — a user with their own
    # repo-local identity keeps it. See git_versioning.py.
    git_author_name: str = "HA User"
    git_author_email: str = "ha@distributed-esphome.local"
    job_history_retention_days: int = 365
    firmware_cache_max_gb: float = 2.0
    # Bug #198: time-based eviction for /data/firmware/. The cache_max_gb
    # ceiling rarely fires in practice — at typical fleet activity the
    # firmware dir sits well under 2 GiB, so a year of binaries pile up
    # waiting for a budget pass that never comes (and ride along in
    # every HA backup). 2 days is enough for the most common
    # "compile-now-flash-later" use case while keeping the on-disk
    # footprint bounded to the recent past. 0 = unlimited.
    firmware_retention_days: int = 2
    job_log_retention_days: int = 30
    # --- Fields migrated from Supervisor options.json in 1.6 (SP.8) ---
    # Shared Bearer token used by workers and (when require_ha_auth is
    # true) direct-port UI access. Empty = auto-generate on first boot.
    server_token: str = ""
    # Seconds a compile job may run before the server marks it timed-out.
    job_timeout: int = 600
    # Seconds for OTA upload after compile.
    ota_timeout: int = 120
    # Seconds without a worker heartbeat before it's flagged offline.
    worker_offline_threshold: int = 30
    # Seconds between ESPHome-device API polls (online / running version).
    device_poll_interval: int = 60
    # #238: when False (default), the device poller trusts mDNS for liveness +
    # ``running_version`` and only opens an ``aioesphomeapi`` connection on
    # first sight (to backfill ``mac_address`` + ``compilation_time``), as a
    # fallback for devices the mDNS browser hasn't seen recently (Ethernet,
    # OpenThread, ``mdns: enabled: false``), or via the post-OTA refresh hook.
    # When True, every tick fans out an API query to every known device — the
    # pre-1.7.1 behaviour. Reported by pricklyguy in #143: the every-60-s
    # blanket query churned the device's ``api.connection`` log, fired
    # ``on_connect:`` automations, and pressured ``reboot_timeout`` on devices
    # whose HA persistent connection competed for the same client slot. Power
    # users diagnosing a flaky device can flip this back on transiently.
    device_native_api_poll: bool = False
    # When true, direct-port access on :8765 (outside Ingress) requires
    # a valid HA Bearer or the add-on's own server token. The dataclass
    # default is ``False`` so standalone Docker installs (no Supervisor →
    # no way to validate an HA Bearer) stay reachable out of the box
    # (bug #83). On HAOS fresh installs (SUPERVISOR_TOKEN present),
    # ``_seed_from_options`` upgrades the runtime default to ``True`` so
    # direct port 8765 actually 401s (bug #87). Ingress access never
    # consults this flag — path 1 (Supervisor peer trust) in
    # ``ha_auth.py`` short-circuits first. Existing installs that
    # explicitly set a value in ``/data/settings.json`` keep it.
    require_ha_auth: bool = False
    # #82 / UX_REVIEW §3.10 — time-of-day presentation for Queue,
    # History, and log timestamps. ``'auto'`` defers to the browser's
    # resolved locale (``Intl.DateTimeFormat().resolvedOptions().hour12``);
    # ``'12h'`` / ``'24h'`` force the format regardless of locale. The
    # UI reads this via ``GET /ui/api/settings`` and applies it through
    # ``utils/format.ts``.
    time_format: str = "auto"

    # Bug #5: date presentation for absolute dates in Queue / History tabs.
    # Same shape as time_format: ``'auto'`` defers to the browser's resolved
    # locale; the explicit values force a specific style regardless. Wired
    # through ``utils/format.ts::setDateFormatPref`` from App.tsx on settings
    # load and on every drawer commit.
    date_format: str = "auto"

    # I18N.2 (#141): UI language preference. ``'auto'`` resolves to
    # ``navigator.language`` in the browser; explicit ``'en'`` / ``'de'``
    # force the locale regardless of the browser setting. Wired through
    # ``i18next.changeLanguage()`` from App.tsx. Adding a language here is
    # not enough — its catalog also has to ship in
    # ``ha-addon/ui/src/i18n/locales/`` and the ``_validate_enum`` call
    # below has to enumerate it.
    language: str = "auto"

    # #145: UI font-size scale. ``'normal'`` = today's sizing (default,
    # byte-identical render to pre-#145); ``'small'`` shrinks the whole UI
    # proportionally for users running HA at a Brave/Firefox/Edge zoom
    # below 100 % (Wolfgang-TH runs Brave at 80 % to match HA); ``'large'``
    # is the accessibility step up. Wired through App.tsx by setting
    # ``data-font-size`` on the root element; CSS in index.css picks up
    # ``html[data-font-size="small"]`` etc. and shifts the Tailwind base
    # font-size variable.
    font_size: str = "normal"

    # DQ.1: fleet-wide default per-worker disk quota for the
    # ``/esphome-versions/`` tree (venvs + per-target caches + per-slot
    # working dirs + pio-slot toolchains). Pushed to every worker on every
    # heartbeat as ``HeartbeatResponse.set_disk_quota_bytes``; per-worker
    # overrides in ``WorkerDiskQuotaStore`` win over this default. 10 GiB
    # is enough for 1 venv + a handful of per-target caches + 1 toolchain
    # without wasting Pi-class storage.
    default_worker_disk_quota_bytes: int = 10 * 1024 ** 3


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise SettingsValidationError(field, f"expected bool, got {value!r}")


def _validate_int_range(
    lo: int, hi: int, *, multiple_of: int | None = None,
) -> Callable[[Any, str], int]:
    def _v(value: Any, field: str) -> int:
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            raise SettingsValidationError(field, f"expected integer, got {value!r}")
        if coerced < lo or coerced > hi:
            raise SettingsValidationError(field, f"must be between {lo} and {hi}, got {coerced}")
        if multiple_of is not None and coerced % multiple_of != 0:
            raise SettingsValidationError(
                field, f"must be a multiple of {multiple_of}, got {coerced}",
            )
        return coerced

    return _v


def _validate_float_range(lo: float, hi: float) -> Callable[[Any, str], float]:
    def _v(value: Any, field: str) -> float:
        try:
            coerced = float(value)
        except (TypeError, ValueError):
            raise SettingsValidationError(field, f"expected number, got {value!r}")
        if coerced < lo or coerced > hi:
            raise SettingsValidationError(field, f"must be between {lo} and {hi}, got {coerced}")
        return coerced

    return _v


def _validate_enum(*allowed: str) -> Callable[[Any, str], str]:
    """#82: string-enum validator — accepts one of a fixed set of labels."""
    allowed_set = set(allowed)
    def _v(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise SettingsValidationError(field, f"expected string, got {type(value).__name__}")
        if value not in allowed_set:
            raise SettingsValidationError(field, f"must be one of {sorted(allowed_set)}, got {value!r}")
        return value
    return _v


def _validate_str(max_len: int) -> Callable[[Any, str], str]:
    def _v(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise SettingsValidationError(field, f"expected string, got {type(value).__name__}")
        stripped = value.strip()
        if not stripped:
            raise SettingsValidationError(field, "must not be empty")
        if len(stripped) > max_len:
            raise SettingsValidationError(field, f"must be {max_len} characters or fewer")
        return stripped

    return _v


def _validate_token(value: Any, field: str) -> str:
    """Tokens: non-empty string, loose length cap, no whitespace.

    Empty server token is permitted only through the import/auto-generate
    path, not via a direct PATCH — users shouldn't be able to blank the
    token out from the drawer.
    """
    if not isinstance(value, str):
        raise SettingsValidationError(field, f"expected string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise SettingsValidationError(field, "must not be empty")
    if len(stripped) > 512:
        raise SettingsValidationError(field, "must be 512 characters or fewer")
    if any(c.isspace() for c in stripped):
        raise SettingsValidationError(field, "must not contain whitespace")
    return stripped


# Per-field validators. Any PATCH that names a key not listed here is
# rejected — keeps typos from silently disappearing.
_VALIDATORS: dict[str, Callable[[Any, str], Any]] = {
    "versioning_enabled": _validate_enum("on", "off", "unset"),
    "auto_commit_on_save": _validate_bool,
    # Git author. Don't validate email format — git itself accepts
    # arbitrary strings (e.g. "ha@distributed-esphome.local" isn't a
    # routable email), so requiring an RFC-shaped address would reject
    # legitimate values.
    "git_author_name": _validate_str(100),
    "git_author_email": _validate_str(256),
    # 0 = unlimited is explicitly allowed (matches JH.3 spec). 3650 = 10y.
    "job_history_retention_days": _validate_int_range(0, 3650),
    # Hard floor at 0.1 GB so a typo ("0") doesn't nuke cached firmware.
    "firmware_cache_max_gb": _validate_float_range(0.1, 1024.0),
    # Bug #198: 0 = unlimited; 3650 = 10y. Same shape as the other two
    # retention knobs.
    "firmware_retention_days": _validate_int_range(0, 3650),
    # 0 = unlimited; 3650 = 10y.
    "job_log_retention_days": _validate_int_range(0, 3650),
    # --- SP.8 migrated fields ---
    "server_token": _validate_token,
    # Compile budget: 60s floor (something silly like 5s would time
    # out every real build), 4h ceiling (a compile that long is stuck).
    "job_timeout": _validate_int_range(60, 14400),
    # OTA budget: 15s floor (WiFi handshake alone can take that),
    # 30min ceiling.
    "ota_timeout": _validate_int_range(15, 1800),
    # Worker offline threshold: at least as long as a heartbeat
    # interval (default 10s on the client) + one missed beat, so
    # 15s floor. 1h ceiling — anything longer and the UI lies.
    "worker_offline_threshold": _validate_int_range(15, 3600),
    # Device poll: 10s floor (below that we hammer devices), 1h ceiling.
    "device_poll_interval": _validate_int_range(10, 3600),
    "device_native_api_poll": _validate_bool,
    "require_ha_auth": _validate_bool,
    # #82: enum validator — 'auto' / '12h' / '24h'. See AppSettings.time_format.
    "time_format": _validate_enum("auto", "12h", "24h"),
    # Bug #5: date enum — 'auto' / 'iso' (2026-04-27) / 'us' (4/27/2026)
    # / 'eu' (27/04/2026) / 'long' (Apr 27, 2026).
    "date_format": _validate_enum("auto", "iso", "us", "eu", "long"),
    # I18N.2 (#141): UI locale — 'auto' (browser) / 'en' / 'de'.
    "language": _validate_enum("auto", "en", "de"),
    # #145: font-size scale — 'small' / 'normal' (default) / 'large'.
    "font_size": _validate_enum("small", "normal", "large"),
    # DQ.1: ≥1 GiB floor stops a typo from starving every worker into
    # constant eviction; 1 TiB ceiling matches firmware_cache_max_gb's
    # upper bound (anything bigger is misconfiguration). Also pinned to
    # whole-GiB multiples so the UI's `Math.round(bytes / GiB)` display
    # round-trips cleanly — without this, an API caller could store
    # e.g. 10.5 GiB, the Settings drawer would render "11", and the
    # next save would silently rewrite the value to 11 GiB.
    "default_worker_disk_quota_bytes": _validate_int_range(
        1 * 1024 ** 3, 1024 * 1024 ** 3, multiple_of=1024 ** 3,
    ),
}


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_settings: AppSettings | None = None
_lock: asyncio.Lock | None = None
_settings_path: Path = SETTINGS_FILE
_options_path: Path = OPTIONS_FILE


def _get_lock() -> asyncio.Lock:
    # Lazy-create so import doesn't require a running loop.
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON to *path* (tempfile in same dir + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    """Read JSON object from *path*. Returns ``{}`` on any failure."""
    try:
        raw = path.read_text()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        logger.error("%s is not a JSON object (got %s); ignoring", path, type(parsed).__name__)
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("Failed to read %s; treating as empty", path)
    return {}


def clear_supervisor_options_if_needed() -> None:
    """Bug #9: silence Supervisor's "Option X does not exist in the schema"
    WARNINGs that fire once per option-read after SP.8 stripped the
    schema from config.yaml.

    Supervisor keeps an in-memory copy of the add-on's last-known
    user-configured options (``token``, ``job_timeout``, etc.). After
    SP.8, the schema is empty, so every option read logs a warning for
    each now-unknown key. The fix is to POST an empty options payload
    to ``/addons/self/options`` once after a successful settings
    migration — Supervisor rewrites its cache, the warnings stop.

    Guarded by a marker file so it runs at most once per install. On
    failure (no SUPERVISOR_TOKEN, network error) we log DEBUG and move
    on — the warnings are cosmetic, not load-bearing.
    """
    if SUPERVISOR_OPTIONS_CLEARED_MARKER.exists():
        return

    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        # Not running under Supervisor — nothing to clear.
        return

    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    try:
        req = urllib.request.Request(
            "http://supervisor/addons/self/options",
            data=json.dumps({"options": {}}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (Supervisor-local URL is trusted)
            if 200 <= resp.status < 300:
                logger.info(
                    "Cleared Supervisor's stale options cache (SP.8 schema strip) — warnings should stop"
                )
                try:
                    SUPERVISOR_OPTIONS_CLEARED_MARKER.write_text("1")
                except OSError:
                    logger.exception("Failed to write %s", SUPERVISOR_OPTIONS_CLEARED_MARKER)
            else:
                logger.debug("Supervisor options-clear returned %d", resp.status)
    except Exception:
        logger.debug("Supervisor options-clear failed; will retry next boot", exc_info=True)


# PR #64 review: make the Supervisor-probe failure path audible.
# Pre-fix, a Supervisor that returned 500 or hung quietly downgraded
# to ``/data/options.json`` with no log line, and if a token appeared
# to "rotate" on upgrade the silent fallback was the first thing
# operators had to suspect. We now emit one WARNING on the first
# failure (with reason) and downgrade subsequent calls in the same
# process to DEBUG so boot doesn't spam the log when Supervisor is
# genuinely unreachable.
_supervisor_probe_warned: bool = False


def _read_supervisor_options() -> dict[str, Any]:
    """Fetch this add-on's user-stored options from Supervisor.

    Supervisor persists user options server-side even when the add-on's
    config.yaml has no ``options:``/``schema:`` block (SP.8 state).
    The values in ``/data/options.json`` are Supervisor's schema-driven
    projection of those options — so stripping the schema empties the
    in-container file, but the user's configured values are still
    accessible via this HTTP endpoint.

    Returns ``{}`` on any failure (missing ``SUPERVISOR_TOKEN`` env,
    Supervisor unreachable, unexpected JSON shape). Silent outside the
    HA add-on context — ideal for tests and bare ``python`` runs.
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    global _supervisor_probe_warned

    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        # Not in an HA add-on context — tests, bare python, etc.
        # No warning; this is the expected no-op path.
        return {}
    try:
        req = urllib.request.Request(
            SUPERVISOR_INFO_URL,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (Supervisor-local URL is trusted)
            payload = json.loads(resp.read())
        opts = payload.get("data", {}).get("options", {})
        if isinstance(opts, dict):
            return opts
    except Exception as exc:
        if not _supervisor_probe_warned:
            logger.warning(
                "Supervisor options probe failed (%s: %s); falling back to /data/options.json. "
                "If a token or timeout appears to have reset on upgrade, this is why.",
                type(exc).__name__, exc,
            )
            _supervisor_probe_warned = True
        else:
            logger.debug("Supervisor options probe failed again; ignoring", exc_info=True)
    return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_settings(
    settings_path: Path | None = None,
    options_path: Path | None = None,
    fresh_repo_init: bool | None = None,
) -> AppSettings:
    """Load settings from disk, importing from ``options.json`` on first boot.

    Must be called exactly once at server startup before any consumer
    calls :func:`get_settings`. Idempotent: a second call re-reads the
    file (primarily for tests that mutate disk behind the module's back).

    ``fresh_repo_init`` (bug #19): the boolean returned by
    :func:`git_versioning.init_repo`. When ``True`` it signals Fleet
    just created a brand-new git repo under ``/config/esphome/`` (no
    repo was there before) — the ideal case for auto-commit-on-save.
    When ``False`` the directory was already a git repo owned by the
    user, and we default ``auto_commit_on_save`` to ``False`` so Fleet
    doesn't spray ``save: foo.yaml`` commits into Pat-with-git's
    curated log. ``None`` (not supplied) → keep the dataclass default
    of ``True`` — used by tests and non-Supervisor harnesses that
    don't have a git repo in play.

    Parameters let tests redirect to a scratch directory.
    """

    global _settings, _settings_path, _options_path

    if settings_path is not None:
        _settings_path = settings_path
    if options_path is not None:
        _options_path = options_path

    if _settings_path.exists():
        _settings = _load_from_file()
        logger.info("Loaded settings from %s", _settings_path)
        _settings = _ensure_server_token(_settings)
        return _settings

    # First boot: seed from options.json for migrated fields, keep dataclass
    # defaults for everything else. If the filesystem is read-only (e.g. a
    # test harness running without /data), fall back to in-memory defaults
    # so the server still boots.
    _settings = _seed_from_options()

    # Bug #19 + #98: pre-existing git repo means the user is already
    # versioning their config — flip ``versioning_enabled`` to
    # ``'on'`` (they've already opted in by using git) and keep
    # auto-commit OFF so we don't spray ``save:`` commits into their
    # curated log without consent. They can flip auto-commit on in
    # the Settings drawer if they want the Fleet safety net.
    #
    # Fresh install (``fresh_repo_init`` is True or None) — stay at
    # the dataclass default of ``'unset'``; the UI will prompt the
    # user the first time they open it.
    if fresh_repo_init is False:
        _settings = AppSettings(**{
            **asdict(_settings),
            "auto_commit_on_save": False,
            "versioning_enabled": "on",
        })
        logger.info(
            "Pre-existing git repo detected on first boot; "
            "versioning_enabled='on', auto_commit_on_save=False",
        )

    try:
        _atomic_write(_settings_path, asdict(_settings))
        logger.info("Created %s with defaults (migrated fields imported from %s where present)", _settings_path, _options_path)
    except OSError:
        logger.warning(
            "Could not create %s (read-only fs?); serving defaults in-memory only",
            _settings_path,
        )
    return _settings


def _ensure_server_token(s: AppSettings) -> AppSettings:
    """If ``server_token`` came back empty, recover it from the pre-1.6
    sources before falling back to auto-generation.

    Critical for upgrade continuity: a 1.6.0-dev.N → dev.N+1 bump that
    first introduces ``server_token`` would, without this chain, rotate
    the token on every existing install (new auto-gen value, all
    remote workers 401 until their SERVER_TOKEN env is updated). So
    consult the same sources ``_seed_from_options`` uses, in the same
    priority order, before minting a new token:

    1. options.json[token] — set by the user on pre-1.6 installs
    2. /data/auth_token — pre-1.6 auto-generated token file
    3. ``secrets.token_hex(16)`` — truly fresh install, nothing to
       inherit from

    The recovered/generated value is persisted back so subsequent
    loads don't repeat the rescue work.
    """
    if s.server_token and s.server_token.strip():
        return s

    recovered = _recover_legacy_token()
    if recovered:
        logger.info("Recovered server_token from legacy source; preserving worker auth continuity")
        token = recovered
    else:
        logger.warning("server_token was empty and no legacy source found; generating a new one")
        token = secrets.token_hex(16)

    updated = AppSettings(**{**asdict(s), "server_token": token})
    try:
        _atomic_write(_settings_path, asdict(updated))
    except OSError:
        logger.exception("Failed to persist recovered/regenerated server_token")
    return updated


def _recover_legacy_token() -> str | None:
    """Look up the pre-1.6 token, priority: Supervisor → options.json → auth_token.

    Priority ordering matches the hass-4 incident investigation:
    Supervisor's live record is authoritative (survives config.yaml
    schema strips); ``/data/options.json`` is the on-disk mirror
    (may be empty post-strip); ``/data/auth_token`` is the pre-1.6
    auto-generated-token fallback kept for installs that never set a
    user token.
    """
    sup = _read_supervisor_options()
    tok = sup.get("token", "")
    if isinstance(tok, str) and tok.strip():
        return tok.strip()

    options = _read_json(_options_path)
    tok = options.get("token", "")
    if isinstance(tok, str) and tok.strip():
        return tok.strip()

    if LEGACY_TOKEN_FILE.exists():
        try:
            legacy = LEGACY_TOKEN_FILE.read_text().strip()
            if legacy:
                return legacy
        except OSError:
            logger.exception("Failed to read %s", LEGACY_TOKEN_FILE)
    return None


def _load_from_file() -> AppSettings:
    raw = _read_json(_settings_path)
    # Unknown keys are tolerated on load (forward-compat), but logged at
    # WARNING so they don't rot invisibly.
    known = {f.name for f in fields(AppSettings)}
    for key in sorted(set(raw) - known):
        logger.warning("Unknown key in %s: %r — ignored", _settings_path, key)

    # #98: legacy migration — ``versioning_enabled`` was a bool in
    # dev.38–dev.39. Upgrade those values to the tristate string the
    # rest of the codebase now expects. ``True`` → ``'on'``, ``False``
    # → ``'off'``. Absent key → let the dataclass default ('unset')
    # stand, which means the user will see the onboarding modal.
    if "versioning_enabled" in raw and isinstance(raw["versioning_enabled"], bool):
        migrated = "on" if raw["versioning_enabled"] else "off"
        logger.info(
            "Migrating legacy bool versioning_enabled=%r to tristate %r",
            raw["versioning_enabled"], migrated,
        )
        raw["versioning_enabled"] = migrated

    defaults = AppSettings()
    kwargs: dict[str, Any] = {}
    for f in fields(AppSettings):
        if f.name in raw:
            try:
                kwargs[f.name] = _VALIDATORS[f.name](raw[f.name], f.name)
            except SettingsValidationError as exc:
                logger.error("Invalid value in %s for %s; using default: %s", _settings_path, f.name, exc)
                kwargs[f.name] = getattr(defaults, f.name)
        else:
            kwargs[f.name] = getattr(defaults, f.name)
    return AppSettings(**kwargs)


def _seed_from_options() -> AppSettings:
    """Build initial AppSettings, pulling migrated fields from Supervisor.

    On first boot we want the user's pre-1.6 option values carried over
    intact. Supervisor's HTTP API is the authoritative source — it
    sees everything the user configured in the Supervisor UI regardless
    of the current config.yaml schema (hass-4 incident: stripping the
    schema in SP.8 emptied /data/options.json server-side, so reading
    only that file lost every option). We merge in this order, later
    wins:

    1. ``/data/options.json`` — legacy on-disk file (kept so installs
       still work if Supervisor isn't reachable).
    2. ``http://supervisor/addons/self/info`` — the live source of
       truth for what the user actually has configured.

    Token migration continues to use the legacy-recovery helper
    (Supervisor API → options.json → /data/auth_token → auto-generate)
    so dev-loop / non-Supervisor installs still work.
    """
    defaults = AppSettings()
    options = {**_read_json(_options_path), **_read_supervisor_options()}
    kwargs: dict[str, Any] = asdict(defaults)
    imported: list[str] = []
    for key in IMPORT_FROM_OPTIONS:
        if key in options:
            try:
                kwargs[key] = _VALIDATORS[key](options[key], key)
                imported.append(key)
            except SettingsValidationError as exc:
                logger.warning("Could not import %s: %s", key, exc)

    # Bug #87: on HAOS fresh installs (SUPERVISOR_TOKEN present, no
    # explicit `require_ha_auth` in options), default to True so direct
    # port 8765 access returns 401. Bug #83 flipped the dataclass
    # default to False to unblock standalone Docker; this re-enables
    # secure-by-default for Ingress-wrapped installs without reopening
    # the standalone-Docker lockout. Ingress paths always short-circuit
    # in `ha_auth.ha_auth_middleware` via the Supervisor peer-IP trust
    # path (see ha_auth.py path 1) — this only affects the direct port.
    if "require_ha_auth" not in imported and os.environ.get("SUPERVISOR_TOKEN"):
        kwargs["require_ha_auth"] = True
        imported.append("require_ha_auth=true (HAOS default, #87)")

    recovered = _recover_legacy_token()
    if recovered:
        kwargs["server_token"] = recovered
        imported.append("server_token from legacy source")
    else:
        kwargs["server_token"] = secrets.token_hex(16)
        logger.info(
            "Generated new server token (no existing token found in options.json, Supervisor, or %s)",
            LEGACY_TOKEN_FILE,
        )

    if imported:
        logger.info("Imported options (Supervisor + options.json): %s", ", ".join(imported))
    return AppSettings(**kwargs)


def get_settings() -> AppSettings:
    """Return the current settings singleton.

    Cheap and safe — consumers should call this at decision time so
    changes made via :func:`update_settings` propagate without restart.
    """

    if _settings is None:
        # Defensive: if a code path reads settings before init_settings()
        # has run (e.g., an import-time access), return defaults so we
        # don't crash. Startup logs will flag the ordering issue.
        logger.warning("get_settings() called before init_settings(); returning defaults")
        return AppSettings()
    return _settings


async def update_settings(partial: dict[str, Any]) -> AppSettings:
    """Validate, persist, and apply a partial settings update.

    Unknown keys raise :class:`SettingsValidationError`. Values are
    validated per-field; any failure aborts the entire PATCH (no partial
    application). On success, the file is rewritten atomically and the
    in-memory singleton is replaced.
    """

    global _settings

    if not isinstance(partial, dict):
        raise SettingsValidationError("", "expected a JSON object")

    known = {f.name for f in fields(AppSettings)}
    unknown = set(partial) - known
    if unknown:
        # Pick one offending key for the error; log the rest.
        offender = sorted(unknown)[0]
        raise SettingsValidationError(offender, "unknown settings key")

    # Validate every value first so we don't partially apply.
    validated: dict[str, Any] = {}
    for key, value in partial.items():
        validated[key] = _VALIDATORS[key](value, key)

    async with _get_lock():
        current = get_settings()
        merged = AppSettings(**{**asdict(current), **validated})
        _atomic_write(_settings_path, asdict(merged))
        _settings = merged
        logger.info("Settings updated: %s", ", ".join(f"{k}={v!r}" for k, v in validated.items()))
        return merged


def settings_as_dict() -> dict[str, Any]:
    """Convenience: return the current settings as a plain dict."""
    return asdict(get_settings())


def _reset_for_tests() -> None:
    """Test-only: reset module state. Not part of the public API."""
    global _settings, _lock, _settings_path, _options_path, _supervisor_probe_warned
    _settings = None
    _lock = None
    _settings_path = SETTINGS_FILE
    _options_path = OPTIONS_FILE
    _supervisor_probe_warned = False


def _set_for_tests(**overrides: Any) -> None:
    """Test-only: synchronously set fields on the singleton.

    Most consumers read ``get_settings().xxx`` live; tests that need
    specific values without the async ``update_settings`` overhead
    (e.g. a sync test helper building an aiohttp app) can use this to
    seed the singleton directly. Creates the singleton if absent.
    """
    global _settings
    current = asdict(_settings) if _settings else asdict(AppSettings())
    current.update(overrides)
    _settings = AppSettings(**current)
