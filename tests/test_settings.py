"""Unit tests for the in-app Settings store (SP.*).

Covers:
- Dataclass defaults.
- First-boot creation with dataclass defaults when no options.json.
- First-boot import from options.json for migrated fields (SP.2).
- Subsequent boots don't re-import.
- Round-trip load/save.
- Atomic write leaves no half-written settings.json on simulated crash.
- update_settings() validates + persists + rotates the singleton.
- Validation errors raise SettingsValidationError with the field name.
- Unknown keys in PATCH are rejected.
- get_settings() sees mutations immediately (live-effect floor).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import settings as settings_mod
from settings import (
    AppSettings,
    SettingsValidationError,
    get_settings,
    init_settings,
    settings_as_dict,
    update_settings,
)


@pytest.fixture(autouse=True)
def _reset():
    """Reset the module singleton between tests."""
    settings_mod._reset_for_tests()
    yield
    settings_mod._reset_for_tests()


# ---------------------------------------------------------------------------
# Defaults + first-boot
# ---------------------------------------------------------------------------


def test_dataclass_defaults_match_spec():
    s = AppSettings()
    assert s.auto_commit_on_save is True
    assert s.git_author_name == "HA User"
    assert s.git_author_email == "ha@distributed-esphome.local"
    assert s.job_history_retention_days == 365
    assert s.firmware_cache_max_gb == 2.0
    assert s.job_log_retention_days == 30
    assert s.default_worker_disk_quota_bytes == 10 * 1024 ** 3


# ---------------------------------------------------------------------------
# DQ.14 — default_worker_disk_quota_bytes validator
# ---------------------------------------------------------------------------


async def test_update_settings_accepts_disk_quota_bytes(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    updated = await update_settings({"default_worker_disk_quota_bytes": 5 * 1024 ** 3})
    assert updated.default_worker_disk_quota_bytes == 5 * 1024 ** 3


async def test_update_settings_rejects_disk_quota_below_floor(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    # Anything below 1 GiB is rejected (typo guard — a "0" would starve workers).
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"default_worker_disk_quota_bytes": 1024 ** 3 - 1})
    assert exc.value.field == "default_worker_disk_quota_bytes"


async def test_update_settings_rejects_disk_quota_zero(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"default_worker_disk_quota_bytes": 0})
    assert exc.value.field == "default_worker_disk_quota_bytes"


async def test_update_settings_rejects_disk_quota_negative(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"default_worker_disk_quota_bytes": -1024 ** 3})
    assert exc.value.field == "default_worker_disk_quota_bytes"


async def test_update_settings_rejects_disk_quota_above_ceiling(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    # Above 1 TiB is rejected as misconfiguration.
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"default_worker_disk_quota_bytes": (1024 + 1) * 1024 ** 3})
    assert exc.value.field == "default_worker_disk_quota_bytes"


async def test_update_settings_accepts_disk_quota_at_floor(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    updated = await update_settings({"default_worker_disk_quota_bytes": 1 * 1024 ** 3})
    assert updated.default_worker_disk_quota_bytes == 1 * 1024 ** 3


async def test_update_settings_accepts_disk_quota_at_ceiling(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    updated = await update_settings({"default_worker_disk_quota_bytes": 1024 * 1024 ** 3})
    assert updated.default_worker_disk_quota_bytes == 1024 * 1024 ** 3


async def test_update_settings_accepts_git_author(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    updated = await update_settings({
        "git_author_name": "Stefan Zier",
        "git_author_email": "stefan@zier.com",
    })
    assert updated.git_author_name == "Stefan Zier"
    assert updated.git_author_email == "stefan@zier.com"


async def test_update_settings_trims_git_author_whitespace(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    updated = await update_settings({"git_author_name": "  Stefan Zier  "})
    assert updated.git_author_name == "Stefan Zier"


async def test_update_settings_rejects_empty_git_author_name(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"git_author_name": "   "})
    assert exc.value.field == "git_author_name"


async def test_update_settings_rejects_overlong_git_author_email(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"git_author_email": "a" * 500 + "@x.com"})
    assert exc.value.field == "git_author_email"


# ---------------------------------------------------------------------------
# #82 / UX_REVIEW §3.10 — time_format enum
# ---------------------------------------------------------------------------


async def test_update_settings_accepts_time_format_values(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    for val in ("auto", "12h", "24h"):
        updated = await update_settings({"time_format": val})
        assert updated.time_format == val


async def test_update_settings_rejects_unknown_time_format(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"time_format": "military"})
    assert exc.value.field == "time_format"


async def test_time_format_defaults_to_auto(tmp_path):
    s = init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    assert s.time_format == "auto"


# ---------------------------------------------------------------------------
# #98 — versioning_enabled tristate + legacy bool migration
# ---------------------------------------------------------------------------


def test_versioning_enabled_defaults_to_unset(tmp_path):
    """Fresh install → 'unset'; the UI modal asks the user to decide."""
    s = init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    assert s.versioning_enabled == "unset"


def test_versioning_enabled_migrates_legacy_true(tmp_path):
    """dev.38–dev.39 wrote booleans; those installs must upgrade cleanly."""
    settings_file = tmp_path / "s.json"
    settings_file.write_text(json.dumps({
        "versioning_enabled": True,  # legacy bool shape
        "auto_commit_on_save": True,
        "server_token": "x" * 16,
    }))
    s = init_settings(settings_path=settings_file, options_path=tmp_path / "o.json")
    assert s.versioning_enabled == "on"


def test_versioning_enabled_migrates_legacy_false(tmp_path):
    settings_file = tmp_path / "s.json"
    settings_file.write_text(json.dumps({
        "versioning_enabled": False,  # user had it explicitly off
        "auto_commit_on_save": False,
        "server_token": "x" * 16,
    }))
    s = init_settings(settings_path=settings_file, options_path=tmp_path / "o.json")
    assert s.versioning_enabled == "off"


async def test_versioning_enabled_rejects_bool_patch(tmp_path):
    """The PATCH validator enforces the tristate string enum."""
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"versioning_enabled": True})
    assert exc.value.field == "versioning_enabled"


async def test_versioning_enabled_accepts_tristate(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    for val in ("on", "off", "unset"):
        updated = await update_settings({"versioning_enabled": val})
        assert updated.versioning_enabled == val


def test_preexisting_repo_starts_on(tmp_path):
    """If Fleet finds a repo it didn't create, versioning starts 'on' (#98).

    The user is already versioning their config directory via git; not
    auto-enabling would mean Fleet keeps asking the onboarding question
    to someone who's clearly opted in. Auto-commit stays off so we
    don't write to their log uninvited.
    """
    settings_file = tmp_path / "s.json"
    options_file = tmp_path / "o.json"
    s = init_settings(
        settings_path=settings_file,
        options_path=options_file,
        fresh_repo_init=False,  # i.e. repo already existed
    )
    assert s.versioning_enabled == "on"
    assert s.auto_commit_on_save is False


# ---------------------------------------------------------------------------
# SP.8 — migrated Supervisor-options fields
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #87 — require_ha_auth default depends on context (HAOS vs standalone)
# ---------------------------------------------------------------------------


def _stub_supervisor_options(monkeypatch) -> None:
    """Prevent ``settings._read_supervisor_options`` from hitting the
    real ``http://supervisor/addons/self/info`` endpoint during tests.

    Any test that sets ``SUPERVISOR_TOKEN`` triggers ``init_settings →
    _seed_from_options → _read_supervisor_options``, which fires a
    ``urllib.request.urlopen`` with a 5-second timeout. There's no
    Supervisor in CI (or a dev laptop), so the test hangs for 5 s and
    then emits a WARNING log line on first failure. Stub the function
    to return an empty dict so the "no Supervisor-side options"
    semantics still hold but no HTTP goes out.
    """
    monkeypatch.setattr(settings_mod, "_read_supervisor_options", lambda: {})


def test_fresh_install_defaults_require_ha_auth_true_on_haos(tmp_path: Path, monkeypatch):
    """Bug #87: on HAOS fresh install (SUPERVISOR_TOKEN present, no
    explicit require_ha_auth in options), direct port 8765 must 401
    by default. The runtime default flips to True so opening
    http://homeassistant.local:8765 requires auth."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor")
    _stub_supervisor_options(monkeypatch)
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    # No options file at all — pure fresh install.

    s = init_settings(settings_path=settings_file, options_path=options_file)

    assert s.require_ha_auth is True, (
        "#87: HAOS fresh install must default require_ha_auth=True so "
        "direct port 8765 access returns 401"
    )


def test_fresh_install_defaults_require_ha_auth_false_on_standalone(tmp_path: Path, monkeypatch):
    """Bug #83 preserved: standalone Docker (no SUPERVISOR_TOKEN) must
    stay accessible on direct port without auth by default — the user
    has no way to validate HA tokens."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"

    s = init_settings(settings_path=settings_file, options_path=options_file)

    assert s.require_ha_auth is False, (
        "#83: standalone Docker must keep require_ha_auth=False default"
    )


def test_explicit_false_in_options_overrides_haos_default(tmp_path: Path, monkeypatch):
    """When the user (or upgrade path) has an explicit
    require_ha_auth: false in options.json, that wins over the #87
    HAOS default."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor")
    _stub_supervisor_options(monkeypatch)
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"require_ha_auth": False}))

    s = init_settings(settings_path=settings_file, options_path=options_file)

    assert s.require_ha_auth is False, (
        "Explicit options.json value must win over #87 HAOS default"
    )


def test_init_imports_job_timeout_and_friends_from_options(tmp_path: Path):
    """SP.8: job/ota timeouts, thresholds, require_ha_auth migrate on first boot."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({
        "token": "abcdef1234567890abcdef1234567890",
        "job_timeout": 1800,
        "ota_timeout": 300,
        "worker_offline_threshold": 90,
        "device_poll_interval": 120,
        "require_ha_auth": False,
    }))

    s = init_settings(settings_path=settings_file, options_path=options_file)

    assert s.server_token == "abcdef1234567890abcdef1234567890"
    assert s.job_timeout == 1800
    assert s.ota_timeout == 300
    assert s.worker_offline_threshold == 90
    assert s.device_poll_interval == 120
    assert s.require_ha_auth is False


def test_init_imports_legacy_auth_token_file_when_options_token_missing(tmp_path: Path):
    """Pre-1.6 auto-generated tokens live in /data/auth_token; we honour them once."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    legacy_token_file = tmp_path / "auth_token"
    legacy_token_file.write_text("legacy-token-abc\n")

    # Redirect LEGACY_TOKEN_FILE to our tmp path for this one test.
    import settings as settings_mod_inner
    orig = settings_mod_inner.LEGACY_TOKEN_FILE
    settings_mod_inner.LEGACY_TOKEN_FILE = legacy_token_file
    try:
        s = init_settings(settings_path=settings_file, options_path=options_file)
    finally:
        settings_mod_inner.LEGACY_TOKEN_FILE = orig

    assert s.server_token == "legacy-token-abc"


def test_init_generates_fresh_token_when_nothing_configured(tmp_path: Path):
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"

    s = init_settings(settings_path=settings_file, options_path=options_file)

    # secrets.token_hex(16) = 32-char hex string
    assert len(s.server_token) == 32
    assert all(c in "0123456789abcdef" for c in s.server_token)


def test_load_auto_heals_empty_server_token(tmp_path: Path):
    """Settings file present but with no server_token: auto-generate + persist."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    # Write a valid settings.json but with server_token blank.
    settings_file.write_text(json.dumps({
        "auto_commit_on_save": True,
        "server_token": "",
    }))

    s = init_settings(settings_path=settings_file, options_path=options_file)

    assert s.server_token
    on_disk = json.loads(settings_file.read_text())
    assert on_disk["server_token"] == s.server_token


def test_load_auto_heal_prefers_legacy_auth_token_over_generated(tmp_path: Path):
    """Regression: dev.7→dev.8 upgrade on hass-4 rotated tokens because
    auto-heal generated a fresh value instead of looking at the legacy
    ``/data/auth_token`` first. Now it consults legacy sources before
    minting a new token so remote workers don't lose auth on upgrade.
    """
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    legacy_token_file = tmp_path / "auth_token"
    legacy_token_file.write_text("pre-1.6-token-that-workers-know\n")

    # settings.json exists (simulating an earlier 1.6.0-dev.N boot that
    # didn't yet know about server_token) — server_token is absent
    # from the file entirely, so the load path lands on empty default.
    settings_file.write_text(json.dumps({
        "auto_commit_on_save": True,
    }))

    import settings as settings_mod_inner
    orig = settings_mod_inner.LEGACY_TOKEN_FILE
    settings_mod_inner.LEGACY_TOKEN_FILE = legacy_token_file
    try:
        s = init_settings(settings_path=settings_file, options_path=options_file)
    finally:
        settings_mod_inner.LEGACY_TOKEN_FILE = orig

    # Token preserved from legacy file, NOT regenerated.
    assert s.server_token == "pre-1.6-token-that-workers-know"


def test_load_auto_heal_prefers_options_json_token_over_legacy_file(tmp_path: Path):
    """Options.json[token] beats /data/auth_token during auto-heal."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    legacy_token_file = tmp_path / "auth_token"
    options_file.write_text(json.dumps({"token": "token-from-options-json"}))
    legacy_token_file.write_text("pre-1.6-legacy")

    settings_file.write_text(json.dumps({"auto_commit_on_save": True}))

    import settings as settings_mod_inner
    orig = settings_mod_inner.LEGACY_TOKEN_FILE
    settings_mod_inner.LEGACY_TOKEN_FILE = legacy_token_file
    try:
        s = init_settings(settings_path=settings_file, options_path=options_file)
    finally:
        settings_mod_inner.LEGACY_TOKEN_FILE = orig

    assert s.server_token == "token-from-options-json"


def test_init_imports_token_and_options_from_supervisor_api(tmp_path: Path):
    """Hass-4 regression: on the dev.8 deploy Supervisor's own record of
    the user's options (token + timeouts + thresholds) was the only
    remaining source of truth after stripping the config.yaml schema.
    Our import must query the Supervisor HTTP API so the user's pre-1.6
    token isn't silently rotated on upgrade.
    """
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    # options.json is empty (Supervisor no longer populates it post-schema-strip).
    options_file.write_text("{}")

    import settings as settings_mod_inner
    real_read_supervisor = settings_mod_inner._read_supervisor_options

    def fake_supervisor():
        return {
            "token": "supervisor-preserved-token",
            "job_timeout": 1800,
            "ota_timeout": 240,
            "worker_offline_threshold": 45,
            "device_poll_interval": 90,
            "require_ha_auth": True,
        }

    settings_mod_inner._read_supervisor_options = fake_supervisor
    try:
        s = init_settings(settings_path=settings_file, options_path=options_file)
    finally:
        settings_mod_inner._read_supervisor_options = real_read_supervisor

    assert s.server_token == "supervisor-preserved-token"
    assert s.job_timeout == 1800
    assert s.ota_timeout == 240
    assert s.worker_offline_threshold == 45
    assert s.device_poll_interval == 90


def test_auto_heal_pulls_token_from_supervisor_before_generating(tmp_path: Path):
    """Regression for the exact hass-4 incident: settings.json exists from
    an earlier 1.6 dev boot that didn't know about server_token. Auto-heal
    MUST consult Supervisor before minting a fresh token."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    options_file.write_text("{}")
    settings_file.write_text(json.dumps({"auto_commit_on_save": True}))

    import settings as settings_mod_inner
    real = settings_mod_inner._read_supervisor_options
    settings_mod_inner._read_supervisor_options = lambda: {
        "token": "2416d179b5d41bca62091f681065bca9"
    }
    try:
        s = init_settings(settings_path=settings_file, options_path=options_file)
    finally:
        settings_mod_inner._read_supervisor_options = real

    assert s.server_token == "2416d179b5d41bca62091f681065bca9"


def test_supervisor_probe_silent_no_op_without_token_env(tmp_path: Path, monkeypatch):
    """Outside Supervisor (tests, dev) the probe must return {} cleanly."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    import settings as settings_mod_inner
    assert settings_mod_inner._read_supervisor_options() == {}


async def test_update_settings_rejects_empty_or_whitespace_token(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    for bad in ("", "   ", "\t"):
        with pytest.raises(SettingsValidationError) as exc:
            await update_settings({"server_token": bad})
        assert exc.value.field == "server_token"


async def test_update_settings_rejects_token_with_whitespace(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"server_token": "my token here"})
    assert exc.value.field == "server_token"


async def test_update_settings_accepts_new_token(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    updated = await update_settings({"server_token": "new-token-0123456789"})
    assert updated.server_token == "new-token-0123456789"


async def test_update_settings_rejects_job_timeout_below_floor(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"job_timeout": 5})
    assert exc.value.field == "job_timeout"


async def test_update_settings_accepts_reasonable_timeouts(tmp_path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    updated = await update_settings({
        "job_timeout": 1200,
        "ota_timeout": 240,
        "worker_offline_threshold": 60,
        "device_poll_interval": 30,
        "require_ha_auth": False,
    })
    assert updated.job_timeout == 1200
    assert updated.ota_timeout == 240
    assert updated.worker_offline_threshold == 60
    assert updated.device_poll_interval == 30
    assert updated.require_ha_auth is False


def test_init_creates_settings_file_when_absent(tmp_path: Path):
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"  # doesn't exist

    s = init_settings(settings_path=settings_file, options_path=options_file)

    assert settings_file.exists()
    on_disk = json.loads(settings_file.read_text())
    # Server token is auto-generated on first boot — assert shape, not
    # the exact 32-char hex value.
    assert len(on_disk.pop("server_token")) == 32
    assert on_disk == {
        "versioning_enabled": "unset",
        "auto_commit_on_save": True,
        "git_author_name": "HA User",
        "git_author_email": "ha@distributed-esphome.local",
        "job_history_retention_days": 365,
        "firmware_cache_max_gb": 2.0,
        "firmware_retention_days": 2,
        "job_log_retention_days": 30,
        "job_timeout": 600,
        "ota_timeout": 120,
        "worker_offline_threshold": 30,
        "device_poll_interval": 60,
        "device_native_api_poll": False,
        "require_ha_auth": False,
        "time_format": "auto",
        "date_format": "auto",
        "default_worker_disk_quota_bytes": 10 * 1024 ** 3,
    }
    # Everything else matches the dataclass defaults.
    assert s.auto_commit_on_save is True
    assert s.job_timeout == 600
    # #83: default flipped to False in 1.6.2 — standalone Docker
    # installs must be reachable without a token out of the box.
    assert s.require_ha_auth is False
    assert s.server_token  # auto-generated, non-empty


def test_init_imports_migrated_fields_from_options_json(tmp_path: Path):
    """SP.2: first boot seeds migrated fields from options.json."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({
        # Migrated fields:
        "job_history_retention_days": 90,
        "firmware_cache_max_gb": 5.0,
        "job_log_retention_days": 7,
        # Non-migrated — should be ignored:
        "token": "abc",
        "worker_offline_threshold": 60,
    }))

    s = init_settings(settings_path=settings_file, options_path=options_file)

    assert s.job_history_retention_days == 90
    assert s.firmware_cache_max_gb == 5.0
    assert s.job_log_retention_days == 7
    # Not imported: dataclass default preserved
    assert s.auto_commit_on_save is True

    on_disk = json.loads(settings_file.read_text())
    assert on_disk["job_history_retention_days"] == 90
    assert on_disk["firmware_cache_max_gb"] == 5.0


def test_init_does_not_reimport_on_subsequent_boots(tmp_path: Path):
    """SP.2: idempotent — once settings.json exists, options.json is ignored."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"

    # Seed settings.json with a specific value.
    settings_file.write_text(json.dumps({
        "auto_commit_on_save": False,
        "job_history_retention_days": 30,
        "firmware_cache_max_gb": 1.0,
        "job_log_retention_days": 5,
    }))
    # options.json has very different values — must be ignored.
    options_file.write_text(json.dumps({
        "job_history_retention_days": 999,
        "firmware_cache_max_gb": 99.0,
    }))

    s = init_settings(settings_path=settings_file, options_path=options_file)

    assert s.auto_commit_on_save is False
    assert s.job_history_retention_days == 30
    assert s.firmware_cache_max_gb == 1.0


def test_init_tolerates_invalid_option_values_during_import(tmp_path: Path):
    """Garbage in options.json shouldn't crash startup."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({
        "job_history_retention_days": "not-a-number",
        "firmware_cache_max_gb": -1.0,  # below floor
    }))

    s = init_settings(settings_path=settings_file, options_path=options_file)

    # Invalid imports fall back to dataclass defaults, don't crash.
    assert s.job_history_retention_days == 365
    assert s.firmware_cache_max_gb == 2.0


def test_init_tolerates_malformed_settings_file(tmp_path: Path):
    """Load-time: corrupt JSON leaves us with defaults rather than crashing.

    Token is auto-healed from the empty default, so assert shape rather
    than exact equality with AppSettings().
    """
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    settings_file.write_text("not json at all {")

    s = init_settings(settings_path=settings_file, options_path=options_file)

    # Non-token fields equal the dataclass defaults.
    defaults = AppSettings()
    for f in ("auto_commit_on_save", "job_timeout", "ota_timeout", "require_ha_auth"):
        assert getattr(s, f) == getattr(defaults, f)
    # Token was auto-generated.
    assert s.server_token
    assert len(s.server_token) == 32


def test_init_tolerates_invalid_value_in_settings_file(tmp_path: Path):
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    settings_file.write_text(json.dumps({
        "auto_commit_on_save": True,
        "job_history_retention_days": -5,  # below floor
        "firmware_cache_max_gb": 2.0,
        "job_log_retention_days": 30,
    }))

    s = init_settings(settings_path=settings_file, options_path=options_file)

    # Invalid value falls back to default, other values load correctly.
    assert s.job_history_retention_days == 365
    assert s.auto_commit_on_save is True


def test_init_ignores_unknown_keys_in_settings_file(tmp_path: Path, caplog):
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    settings_file.write_text(json.dumps({
        "auto_commit_on_save": False,
        "future_feature_flag": True,  # not in dataclass
    }))

    with caplog.at_level("WARNING"):
        s = init_settings(settings_path=settings_file, options_path=options_file)

    assert s.auto_commit_on_save is False
    assert any("future_feature_flag" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# update_settings (PATCH)
# ---------------------------------------------------------------------------


async def test_update_settings_persists_and_rotates_singleton(tmp_path: Path):
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    init_settings(settings_path=settings_file, options_path=options_file)

    assert get_settings().auto_commit_on_save is True

    updated = await update_settings({"auto_commit_on_save": False})

    assert updated.auto_commit_on_save is False
    # Singleton updated:
    assert get_settings().auto_commit_on_save is False
    # Disk updated:
    on_disk = json.loads(settings_file.read_text())
    assert on_disk["auto_commit_on_save"] is False


async def test_update_settings_partial_leaves_unspecified_unchanged(tmp_path: Path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    await update_settings({"job_history_retention_days": 90})
    s = get_settings()
    assert s.job_history_retention_days == 90
    assert s.auto_commit_on_save is True  # unchanged
    assert s.firmware_cache_max_gb == 2.0  # unchanged


async def test_update_settings_rejects_unknown_key(tmp_path: Path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"totally_fake_key": 1})
    assert exc.value.field == "totally_fake_key"


async def test_update_settings_rejects_out_of_range(tmp_path: Path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"job_history_retention_days": -1})
    assert exc.value.field == "job_history_retention_days"


async def test_update_settings_rejects_non_numeric(tmp_path: Path):
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError) as exc:
        await update_settings({"firmware_cache_max_gb": "lots"})
    assert exc.value.field == "firmware_cache_max_gb"


async def test_update_settings_coerces_string_bool(tmp_path: Path):
    """HA options.json sometimes delivers booleans as strings; tolerate that."""
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    updated = await update_settings({"auto_commit_on_save": "false"})
    assert updated.auto_commit_on_save is False


async def test_update_settings_aborts_on_any_invalid_field(tmp_path: Path):
    """No partial application — one bad field kills the whole PATCH."""
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    with pytest.raises(SettingsValidationError):
        await update_settings({
            "auto_commit_on_save": False,             # valid
            "job_history_retention_days": -1,         # invalid
        })
    # auto_commit_on_save should NOT have been applied.
    assert get_settings().auto_commit_on_save is True


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_tempfile_on_success(tmp_path: Path):
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    init_settings(settings_path=settings_file, options_path=options_file)

    # Only settings.json should be in the directory (no temp files).
    contents = [p.name for p in tmp_path.iterdir()]
    assert "settings.json" in contents
    assert not any(p.startswith("settings.json.") for p in contents)


def test_atomic_write_failure_does_not_corrupt_existing_file(tmp_path: Path):
    """If os.replace raises, the existing settings.json must survive intact."""
    settings_file = tmp_path / "settings.json"
    options_file = tmp_path / "options.json"
    init_settings(settings_path=settings_file, options_path=options_file)

    original_content = settings_file.read_text()

    with patch("settings.os.replace", side_effect=OSError("simulated disk error")):
        with pytest.raises(OSError):
            settings_mod._atomic_write(settings_file, {"bogus": 1})

    # Original content intact.
    assert settings_file.read_text() == original_content
    # No orphaned tempfile.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("settings.json.")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Live-effect (SP.5 floor)
# ---------------------------------------------------------------------------


async def test_get_settings_sees_update_immediately(tmp_path: Path):
    """The whole point of the Settings design — reads after a PATCH see new value."""
    init_settings(settings_path=tmp_path / "s.json", options_path=tmp_path / "o.json")
    assert get_settings().job_history_retention_days == 365
    await update_settings({"job_history_retention_days": 10})
    assert get_settings().job_history_retention_days == 10


def test_settings_as_dict_round_trips():
    """settings_as_dict is used by the REST GET handler."""
    with patch("settings._settings", AppSettings(auto_commit_on_save=False, server_token="abc123")):
        out = settings_as_dict()
    assert out == {
        "versioning_enabled": "unset",
        "auto_commit_on_save": False,
        "git_author_name": "HA User",
        "git_author_email": "ha@distributed-esphome.local",
        "job_history_retention_days": 365,
        "firmware_cache_max_gb": 2.0,
        "firmware_retention_days": 2,
        "job_log_retention_days": 30,
        "server_token": "abc123",
        "job_timeout": 600,
        "ota_timeout": 120,
        "worker_offline_threshold": 30,
        "device_poll_interval": 60,
        "device_native_api_poll": False,
        "require_ha_auth": False,
        "time_format": "auto",
        "date_format": "auto",
        "default_worker_disk_quota_bytes": 10 * 1024 ** 3,
    }


def test_get_settings_before_init_returns_defaults_and_warns(caplog):
    """Defensive: wrong ordering shouldn't crash, just log."""
    with caplog.at_level("WARNING"):
        s = get_settings()
    assert s == AppSettings()
    assert any("before init_settings" in r.message for r in caplog.records)
