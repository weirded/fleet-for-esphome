"""IT.3 — /ui/api/* contract test.

The React frontend (``ha-addon/ui/src/types/index.ts``,
``ha-addon/ui/src/api/client.ts``) and the HA custom integration
(``ha-addon/custom_integration/esphome_fleet/*.py``) both read
specific keys out of UI-API JSON responses. Renaming a server field
without updating both consumers silently breaks the UI or the
integration — the reviewer in the 1.5 cycle called this out as a
real regression vector (bug trace lived in WORKITEMS-1.5 CR.*).

This test pins the **minimum** key set each read endpoint must
return. Adding a new optional field is fine; dropping or renaming a
field the consumers already read is a breaking change that trips
one of the ``assert expected <= actual`` assertions below.

Scope:
- GET-only endpoints (the read surface — writes have their own
  integration tests).
- Minimum contracts, not exhaustive. The assertion is ``expected`` is a
  **subset of** the response keys, not exact equality — that way
  additive changes (new optional fields the UI doesn't yet read)
  don't break the contract until a consumer starts depending on them.

Mechanism: shares the ``_make_ui_app`` fixture with ``test_ui_api.py``
so the test runs against the real aiohttp handlers, not a mock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# pytest's rootdir discovery puts ``tests/`` on sys.path, so sibling
# test files are importable by their base name. Using ``tests.``-prefixed
# paths fails on CI because ``tests/`` isn't a Python package (no
# ``__init__.py``) — only on the rootdir-scan path.
from test_ui_api import _UiApp, _make_ui_app, _write_config  # type: ignore[import-not-found]


# -------------------------------------------------------------------------
# Minimum key contracts.
#
# Each entry is (endpoint_path, setup_hook, extractor, required_keys).
# `setup_hook(ta: _UiApp)` seeds whatever state is needed so the endpoint
# returns a non-empty payload we can inspect. `extractor` pulls the dict
# we assert against out of the raw response JSON (list -> first element,
# or a nested field).
# -------------------------------------------------------------------------


def _no_setup(_ta: _UiApp) -> None:
    pass


async def _seed_target(ta: _UiApp) -> None:
    _write_config(ta.config_dir, "bedroom.yaml", "bedroom")


async def _seed_job(ta: _UiApp) -> None:
    await ta.queue.enqueue(
        target="bedroom.yaml",
        esphome_version="2026.4.0",
        run_id="test",
        timeout_seconds=60,
    )


def _first(body: Any) -> dict[str, Any]:
    assert isinstance(body, list) and body, f"expected non-empty list, got {type(body).__name__}"
    assert isinstance(body[0], dict), f"expected dict entries, got {type(body[0]).__name__}"
    return body[0]


def _asdict(body: Any) -> dict[str, Any]:
    assert isinstance(body, dict), f"expected dict, got {type(body).__name__}"
    return body


# The actual contracts. Keep these alphabetical by path for easy review.
CONTRACTS: list[tuple[str, Any, Any, set[str]]] = [
    # Queue list — UI's QueueTab + integration's coordinator both read
    # every field listed below. Missing one of these in the response is
    # a breaking change for both surfaces.
    (
        "/ui/api/queue",
        _seed_job,
        _first,
        {
            "id", "target", "state", "esphome_version",
            "created_at", "finished_at", "assigned_client_id",
            "assigned_hostname", "ota_result", "ota_only",
            "validate_only", "download_only", "has_firmware",
            "retry_count", "is_followup", "scheduled",
            "schedule_kind", "ha_action", "config_hash",
        },
    ),
    # Server info — populates the version pill, addon-update banner, and
    # the integration's coordinator.
    (
        "/ui/api/server-info",
        _no_setup,
        _asdict,
        {"token", "port", "addon_version", "min_image_version"},
    ),
    # Settings round-trip — the drawer reads every field, and leaving
    # one out returns undefined in the UI which loses form state.
    (
        "/ui/api/settings",
        _no_setup,
        _asdict,
        {
            "auto_commit_on_save", "git_author_name", "git_author_email",
            "job_history_retention_days", "firmware_cache_max_gb",
            "firmware_retention_days", "job_log_retention_days",
            "server_token", "job_timeout",
            "ota_timeout", "worker_offline_threshold",
            "device_poll_interval", "require_ha_auth",
            # 1.7.0 additions: time/date formatting and the fleet-wide
            # disk quota are all wired into the Settings drawer; if the
            # server stops returning any of them the drawer field reads
            # `undefined` and loses its form state on next commit.
            "time_format", "date_format",
            "default_worker_disk_quota_bytes",
            # 1.7.2 I18N.2 (#141): UI locale selector — same drawer
            # field-state contract.
            "language",
            # 1.7.2 #145: font-size scale picker — same drawer field-state
            # contract; UI applies via data-font-size on <html>.
            "font_size",
        },
    ),
    # Targets — Devices tab, integration's device builder, and every
    # downstream "find target by filename" lookup reads these fields.
    (
        "/ui/api/targets",
        _seed_target,
        _first,
        {
            "target", "device_name", "friendly_name",
            "online", "running_version", "server_version",
            "has_uncommitted_changes", "last_flashed_config_hash",
            "config_drifted_since_flash", "last_compile",
        },
    ),
]


@pytest.mark.parametrize(
    "path,setup,extractor,required_keys",
    CONTRACTS,
    ids=[entry[0] for entry in CONTRACTS],
)
async def test_ui_api_key_contract(
    tmp_path: Path,
    path: str,
    setup: Any,
    extractor: Any,
    required_keys: set[str],
) -> None:
    """Every endpoint in CONTRACTS must return at least the listed keys.

    Drift caught here is a breaking change for the UI or the integration
    — add a dedicated bug to WORKITEMS rather than deleting/renaming the
    test assertion.
    """
    ta = await _make_ui_app(tmp_path)
    try:
        maybe_coro = setup(ta)
        if maybe_coro is not None:
            await maybe_coro
        resp = await ta.get(path)
        assert resp.status == 200, f"{path} → HTTP {resp.status}"
        body = await resp.json()
        sample = extractor(body)
        missing = required_keys - set(sample.keys())
        assert not missing, (
            f"{path}: response is missing contract keys {sorted(missing)}. "
            f"Either the server dropped the field (breaking change — add a "
            f"bug to WORKITEMS) or this contract needs an update because a "
            f"key was intentionally renamed (update both TS types and this "
            f"test in the same commit)."
        )
    finally:
        await ta.close()


async def test_history_endpoint_contract(tmp_path: Path) -> None:
    """/ui/api/history returns the JobHistoryEntry shape the UI reads."""
    from job_history import JobHistoryDAO
    from job_queue import Job, JobState

    ta = await _make_ui_app(tmp_path)
    try:
        # Wire a DAO into the app so the endpoint has something to query.
        dao = JobHistoryDAO(db_path=tmp_path / "history.db")
        ta.client._server.app["job_history"] = dao
        job = Job(
            id="contract-1",
            target="bedroom.yaml",
            esphome_version="2026.4.0",
            state=JobState.SUCCESS,
            run_id="r1",
            ota_result="success",
            log="ok\n",
        )
        from datetime import datetime, timezone
        job.finished_at = datetime.now(timezone.utc)
        dao.record_terminal(job)

        resp = await ta.get("/ui/api/history?limit=1")
        assert resp.status == 200
        rows = await resp.json()
        assert rows, "expected at least one history row"
        keys = set(rows[0].keys())
        required = {
            "id", "target", "state", "triggered_by",
            "trigger_detail", "download_only", "validate_only",
            "pinned_client_id", "esphome_version",
            "assigned_client_id", "assigned_hostname",
            "submitted_at", "started_at", "finished_at",
            "duration_seconds", "ota_result", "config_hash",
            "retry_count", "log_excerpt", "has_firmware",
            "firmware_variants",
        }
        missing = required - keys
        assert not missing, (
            f"/ui/api/history row is missing contract keys {sorted(missing)}. "
            f"Breaking change — update TS JobHistoryEntry type in the same commit."
        )
    finally:
        await ta.close()
