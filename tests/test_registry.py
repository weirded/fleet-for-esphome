"""Unit tests for WorkerRegistry — register, heartbeat, disable, versioning."""

from __future__ import annotations

import pytest

from registry import WorkerRegistry


@pytest.fixture
def reg():
    return WorkerRegistry()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_returns_client_id(reg):
    client_id = reg.register("host1", "linux/amd64")
    assert client_id is not None
    assert len(client_id) > 0


def test_register_stores_client(reg):
    client_id = reg.register("host1", "linux/amd64")
    worker = reg.get(client_id)
    assert worker is not None
    assert worker.hostname == "host1"
    assert worker.platform == "linux/amd64"


def test_register_stores_client_version(reg):
    client_id = reg.register("host1", "linux/amd64", client_version="0.0.1")
    worker = reg.get(client_id)
    assert worker.client_version == "0.0.1"


def test_register_client_version_none_by_default(reg):
    client_id = reg.register("host1", "linux/amd64")
    worker = reg.get(client_id)
    assert worker.client_version is None


def test_register_stores_tags(reg):
    """TG.1: worker tags ride along with the in-memory Worker record."""
    client_id = reg.register("host1", "linux/amd64", tags=["prod", "linux"])
    worker = reg.get(client_id)
    assert worker.tags == ["prod", "linux"]


def test_register_tags_default_empty(reg):
    client_id = reg.register("host1", "linux/amd64")
    worker = reg.get(client_id)
    assert worker.tags == []


def test_register_tags_to_dict_includes_tags(reg):
    client_id = reg.register("host1", "linux/amd64", tags=["fast"])
    d = reg.get(client_id).to_dict()
    assert d["tags"] == ["fast"]


def test_set_tags_updates_in_memory(reg):
    client_id = reg.register("host1", "linux/amd64", tags=["prod"])
    assert reg.set_tags(client_id, ["staging", "fast"]) is True
    assert reg.get(client_id).tags == ["staging", "fast"]


def test_set_tags_unknown_returns_false(reg):
    assert reg.set_tags("unknown-id", ["a"]) is False


def test_register_re_register_with_tags_replaces(reg):
    """A worker re-registering with the same client_id and new tags keeps
    them — registration is the funnel through which the persistent
    WorkerTagStore decides what to write; whatever the registry receives
    is what the source of truth resolved to."""
    cid = reg.register("host1", "linux/amd64", tags=["prod"])
    reg.register("host1", "linux/amd64", existing_client_id=cid, tags=["staging"])
    assert reg.get(cid).tags == ["staging"]


def test_register_re_register_with_tags_none_preserves(reg):
    """Passing tags=None on re-register means "leave alone" (older worker
    versions that don't send the field still re-register fine)."""
    cid = reg.register("host1", "linux/amd64", tags=["prod"])
    reg.register("host1", "linux/amd64", existing_client_id=cid, tags=None)
    assert reg.get(cid).tags == ["prod"]


def test_register_multiple_clients_unique_ids(reg):
    id1 = reg.register("host1", "linux/amd64")
    id2 = reg.register("host2", "linux/amd64")
    assert id1 != id2


def test_get_all_returns_all_clients(reg):
    reg.register("host1", "linux/amd64")
    reg.register("host2", "linux/arm64")
    workers = reg.get_all()
    assert len(workers) == 2


# ---------------------------------------------------------------------------
# Heartbeat and online detection
# ---------------------------------------------------------------------------

def test_heartbeat_returns_true_for_known_client(reg):
    client_id = reg.register("host1", "linux/amd64")
    assert reg.heartbeat(client_id) is True


def test_heartbeat_returns_false_for_unknown_client(reg):
    assert reg.heartbeat("unknown-id") is False


def test_is_online_after_register(reg):
    client_id = reg.register("host1", "linux/amd64")
    assert reg.is_online(client_id, threshold_secs=30) is True


def test_is_online_unknown_client(reg):
    assert reg.is_online("unknown-id") is False


def test_is_online_respects_threshold(reg):
    client_id = reg.register("host1", "linux/amd64")
    worker = reg.get(client_id)
    # Backdate last_seen far into the past
    from datetime import datetime, timedelta, timezone
    worker.last_seen = datetime.now(timezone.utc) - timedelta(seconds=60)
    assert reg.is_online(client_id, threshold_secs=30) is False
    assert reg.is_online(client_id, threshold_secs=120) is True


# ---------------------------------------------------------------------------
# Disable / enable
# ---------------------------------------------------------------------------

def test_set_disabled_disables_client(reg):
    client_id = reg.register("host1", "linux/amd64")
    assert reg.set_disabled(client_id, True) is True
    worker = reg.get(client_id)
    assert worker.disabled is True


def test_set_disabled_enables_client(reg):
    client_id = reg.register("host1", "linux/amd64")
    reg.set_disabled(client_id, True)
    reg.set_disabled(client_id, False)
    worker = reg.get(client_id)
    assert worker.disabled is False


def test_set_disabled_returns_false_for_unknown(reg):
    assert reg.set_disabled("unknown-id", True) is False


def test_client_not_disabled_by_default(reg):
    client_id = reg.register("host1", "linux/amd64")
    worker = reg.get(client_id)
    assert worker.disabled is False


def test_disable_does_not_affect_online_status(reg):
    """Disabling a worker should not change is_online — it only affects job assignment."""
    client_id = reg.register("host1", "linux/amd64")
    reg.set_disabled(client_id, True)
    assert reg.is_online(client_id, threshold_secs=30) is True


# ---------------------------------------------------------------------------
# Current job tracking
# ---------------------------------------------------------------------------

def test_set_job_stores_job_id(reg):
    client_id = reg.register("host1", "linux/amd64")
    assert reg.set_job(client_id, "job-123") is True
    assert reg.get(client_id).current_job_id == "job-123"


def test_set_job_clears_job_id(reg):
    client_id = reg.register("host1", "linux/amd64")
    reg.set_job(client_id, "job-123")
    reg.set_job(client_id, None)
    assert reg.get(client_id).current_job_id is None


def test_set_job_returns_false_for_unknown(reg):
    assert reg.set_job("unknown-id", "job-123") is False


# ---------------------------------------------------------------------------
# to_dict serialization
# ---------------------------------------------------------------------------

def test_to_dict_includes_all_fields(reg):
    client_id = reg.register("host1", "linux/amd64", client_version="0.0.1")
    d = reg.get(client_id).to_dict()
    assert d["client_id"] == client_id
    assert d["hostname"] == "host1"
    assert d["platform"] == "linux/amd64"
    assert d["client_version"] == "0.0.1"
    assert d["disabled"] is False
    assert d["current_job_id"] is None
    assert "last_seen" in d
    assert d["health_blocked_reason"] is None


# ---------------------------------------------------------------------------
# #219: disk-pressure self-pause (hysteresis)
# ---------------------------------------------------------------------------

def test_heartbeat_with_disk_above_threshold_sets_health_block(reg):
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(client_id, system_info={"disk_used_pct": 96})
    worker = reg.get(client_id)
    assert worker.health_blocked_reason == "disk_full"


def test_heartbeat_below_exit_threshold_clears_block(reg):
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(client_id, system_info={"disk_used_pct": 97})
    assert reg.get(client_id).health_blocked_reason == "disk_full"
    reg.heartbeat(client_id, system_info={"disk_used_pct": 89})
    assert reg.get(client_id).health_blocked_reason is None


def test_heartbeat_in_hysteresis_band_preserves_state(reg):
    """Between EXIT (90) and ENTER (95), state must NOT flip in either direction."""
    client_id_a = reg.register("host-a", "linux/amd64")
    # Start blocked, then heartbeat at 92 — must stay blocked.
    reg.heartbeat(client_id_a, system_info={"disk_used_pct": 96})
    assert reg.get(client_id_a).health_blocked_reason == "disk_full"
    reg.heartbeat(client_id_a, system_info={"disk_used_pct": 92})
    assert reg.get(client_id_a).health_blocked_reason == "disk_full"

    # Start clean, heartbeat at 92 — must stay clean.
    client_id_b = reg.register("host-b", "linux/amd64")
    reg.heartbeat(client_id_b, system_info={"disk_used_pct": 50})
    reg.heartbeat(client_id_b, system_info={"disk_used_pct": 92})
    assert reg.get(client_id_b).health_blocked_reason is None


def test_heartbeat_without_disk_used_pct_does_not_clear_block(reg):
    """If a heartbeat omits disk_used_pct (e.g. older worker, partial info),
    the block stays in place — clearing requires a positive signal."""
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(client_id, system_info={"disk_used_pct": 96})
    assert reg.get(client_id).health_blocked_reason == "disk_full"
    reg.heartbeat(client_id, system_info={"cpu_usage": 5})  # no disk_used_pct
    assert reg.get(client_id).health_blocked_reason == "disk_full"


# ---------------------------------------------------------------------------
# Absolute-free-space floor (bug: a many-TB worker volume at >=95% used still
# has huge absolute headroom — a pure-percentage gate falsely blocks it).
# ---------------------------------------------------------------------------

_GIB = 1024 ** 3


def test_heartbeat_huge_disk_high_pct_high_absolute_free_not_blocked(reg):
    """The bug's exact repro: 25.8 TB disk at 96% used still has ~1176 GB
    free — must NOT block despite crossing the percentage threshold."""
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(
        client_id,
        system_info={"disk_used_pct": 96, "disk_free_bytes": 1176 * _GIB},
    )
    assert reg.get(client_id).health_blocked_reason is None


def test_heartbeat_small_disk_high_pct_low_absolute_free_still_blocked(reg):
    """Regression guard: a small disk at high percentage with genuinely low
    absolute free space must still block, exactly as before this fix."""
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(
        client_id,
        system_info={"disk_used_pct": 96, "disk_free_bytes": 2 * _GIB},
    )
    assert reg.get(client_id).health_blocked_reason == "disk_full"


def test_heartbeat_combined_exit_via_absolute_free_while_in_hysteresis_band(reg):
    """Percentage alone (93%, inside the 90-95 hysteresis band) would not
    clear a block — but absolute free space recovering above the floor
    clears it via the OR-to-exit path."""
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(
        client_id,
        system_info={"disk_used_pct": 96, "disk_free_bytes": 2 * _GIB},
    )
    assert reg.get(client_id).health_blocked_reason == "disk_full"
    reg.heartbeat(
        client_id,
        system_info={"disk_used_pct": 93, "disk_free_bytes": 20 * _GIB},
    )
    assert reg.get(client_id).health_blocked_reason is None


def test_heartbeat_combined_stays_blocked_in_hysteresis_band_when_free_still_low(reg):
    """Complement of the above: inside the hysteresis band AND absolute
    free space still below the floor — neither exit condition is met, so
    it must stay blocked."""
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(
        client_id,
        system_info={"disk_used_pct": 96, "disk_free_bytes": 2 * _GIB},
    )
    assert reg.get(client_id).health_blocked_reason == "disk_full"
    reg.heartbeat(
        client_id,
        system_info={"disk_used_pct": 93, "disk_free_bytes": 2 * _GIB},
    )
    assert reg.get(client_id).health_blocked_reason == "disk_full"


def test_heartbeat_huge_disk_never_blocks_across_pct_oscillation(reg):
    """A huge disk oscillating between 94% and 97% used, with absolute free
    space always far above the floor, must never transition to blocked."""
    client_id = reg.register("host1", "linux/amd64")
    for pct in (94, 97, 95, 94, 97):
        reg.heartbeat(
            client_id,
            system_info={"disk_used_pct": pct, "disk_free_bytes": 1176 * _GIB},
        )
        assert reg.get(client_id).health_blocked_reason is None


def test_heartbeat_disk_free_bytes_absent_matches_legacy_percentage_only_behavior(reg):
    """An old worker that hasn't upgraded to send disk_free_bytes gets the
    exact pre-fix percentage-only hysteresis."""
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(client_id, system_info={"disk_used_pct": 96})
    assert reg.get(client_id).health_blocked_reason == "disk_full"
    reg.heartbeat(client_id, system_info={"disk_used_pct": 92})
    assert reg.get(client_id).health_blocked_reason == "disk_full"  # hysteresis band
    reg.heartbeat(client_id, system_info={"disk_used_pct": 89})
    assert reg.get(client_id).health_blocked_reason is None


def test_heartbeat_disk_free_bytes_non_numeric_falls_back_to_percentage_only(reg):
    """A garbage disk_free_bytes value is treated as absent, not as a crash
    or a silent "always low" — falls back to legacy percentage-only."""
    client_id = reg.register("host1", "linux/amd64")
    reg.heartbeat(
        client_id,
        system_info={"disk_used_pct": 96, "disk_free_bytes": "not-a-number"},
    )
    assert reg.get(client_id).health_blocked_reason == "disk_full"
# Lifecycle: workers persist until deleted
# ---------------------------------------------------------------------------

def test_registry_persists_workers_across_instances(tmp_path):
    path = tmp_path / "workers.json"
    reg1 = WorkerRegistry(path=path)
    client_id = reg1.register("host1", "linux/amd64", tags=["gpu"])
    reg1.set_disabled(client_id, True)

    reg2 = WorkerRegistry(path=path)
    reg2.load()
    worker = reg2.get(client_id)
    assert worker is not None
    assert worker.hostname == "host1"
    assert worker.platform == "linux/amd64"
    assert worker.tags == ["gpu"]
    assert worker.disabled is True


def test_loaded_workers_drop_transient_job_state(tmp_path):
    path = tmp_path / "workers.json"
    reg1 = WorkerRegistry(path=path)
    client_id = reg1.register("host1", "linux/amd64")
    reg1.set_job(client_id, "job-1")
    reg1.save()

    reg2 = WorkerRegistry(path=path)
    reg2.load()
    assert reg2.get(client_id).current_job_id is None


def test_remove_is_persisted(tmp_path):
    path = tmp_path / "workers.json"
    reg1 = WorkerRegistry(path=path)
    client_id = reg1.register("host1", "linux/amd64")
    reg1.remove(client_id)

    reg2 = WorkerRegistry(path=path)
    reg2.load()
    assert reg2.get(client_id) is None


def test_load_tolerates_missing_and_corrupt_file(tmp_path):
    reg = WorkerRegistry(path=tmp_path / "missing.json")
    reg.load()
    assert reg.get_all() == []

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    reg = WorkerRegistry(path=corrupt)
    reg.load()
    assert reg.get_all() == []


def test_heartbeat_persists_last_seen_when_save_is_due(tmp_path):
    path = tmp_path / "workers.json"
    reg1 = WorkerRegistry(path=path)
    client_id = reg1.register("host1", "linux/amd64")
    from datetime import datetime, timedelta, timezone
    reg1.get(client_id).last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
    reg1.save()
    reg1._last_saved_at = 0.0  # force the throttled heartbeat save to fire
    reg1.heartbeat(client_id)

    reg2 = WorkerRegistry(path=path)
    reg2.load()
    assert reg2.is_online(client_id, threshold_secs=30) is True


def test_mark_stopped_keeps_worker_but_makes_it_offline(reg):
    client_id = reg.register("host1", "linux/amd64")
    assert reg.mark_stopped(client_id) is True
    assert reg.get(client_id) is not None
    assert reg.is_online(client_id, threshold_secs=30) is False


def test_mark_stopped_unknown_returns_false(reg):
    assert reg.mark_stopped("ghost") is False


def test_heartbeat_after_mark_stopped_brings_worker_back_online(reg):
    client_id = reg.register("host1", "linux/amd64")
    reg.mark_stopped(client_id)
    reg.heartbeat(client_id)
    assert reg.is_online(client_id, threshold_secs=30) is True


def test_register_without_id_adopts_offline_worker_with_same_hostname(reg):
    first = reg.register("host1", "linux/amd64", tags=["gpu"])
    reg.mark_stopped(first)
    second = reg.register("host1", "linux/arm64")
    assert second == first
    assert len(reg.get_all()) == 1
    worker = reg.get(first)
    assert worker.platform == "linux/arm64"
    assert reg.is_online(first, threshold_secs=30) is True


def test_register_without_id_does_not_adopt_online_worker_with_same_hostname(reg):
    first = reg.register("host1", "linux/amd64")
    second = reg.register("host1", "linux/amd64")
    assert second != first
    assert len(reg.get_all()) == 2
