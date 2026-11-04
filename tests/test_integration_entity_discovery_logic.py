"""#199 — logic tests for ``_discovery.entity_already_registered``.

The helper decides whether each platform's ``_discover()`` should add an
entity for a given unique_id. It has to answer "already there?" about the
*current session*, not about everything HA has ever seen:

* Session-scoped "yes" prevents the duplicate adds #62 was filed for.
* Restart-scoped "no" is what lets per-target / per-worker entities
  reattach to their existing registry rows after an HA restart. The bug
  in #199 was that the persistent entity registry answered "yes" for
  every unique_id on the first tick after a restart, so ``_discover()``
  skipped all of them and HA left the rows unowned — surfaced to users
  as ``unavailable`` + "no longer being provided by the esphome_fleet
  integration".

These tests drive the helper against a fake ``entity_platform`` module
rather than a running HA, so they assert the *decision*, which is the part
that regressed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).parent.parent
_INT_SRC = _REPO_ROOT / "ha-addon" / "custom_integration" / "esphome_fleet"
_INT_PARENT = _INT_SRC.parent
if str(_INT_PARENT) not in sys.path:
    sys.path.insert(0, str(_INT_PARENT))


def _fake_platform(domain: str, unique_ids: list[str]) -> SimpleNamespace:
    """An ``EntityPlatform`` stand-in: a domain plus its live entities."""
    entities = {
        f"{domain}.ent_{i}": SimpleNamespace(unique_id=uid)
        for i, uid in enumerate(unique_ids)
    }
    return SimpleNamespace(domain=domain, entities=entities)


@pytest.fixture
def helper(monkeypatch):
    """Import the helper with the live-platform lookup swapped for a stub.

    Returns ``(entity_already_registered, set_platforms)`` where
    ``set_platforms`` installs the list the helper will iterate.
    """
    from esphome_fleet import _discovery

    platforms: list[SimpleNamespace] = []

    def _async_get_platforms(hass, domain):  # noqa: ARG001 — signature parity
        return list(platforms)

    monkeypatch.setattr(
        _discovery.entity_platform, "async_get_platforms", _async_get_platforms
    )

    def set_platforms(*plats: SimpleNamespace) -> None:
        platforms[:] = plats

    return _discovery.entity_already_registered, set_platforms


def test_live_entity_on_same_platform_is_reported_registered(helper):
    """Within one session a live entity blocks a second add (#62)."""
    entity_already_registered, set_platforms = helper
    set_platforms(_fake_platform("sensor", ["entry-42_target_porch.yaml_pinned"]))

    assert entity_already_registered(
        MagicMock(), "sensor", "entry-42_target_porch.yaml_pinned"
    )


def test_no_live_platforms_means_not_registered(helper):
    """#199: the state right after an HA restart — registry rows exist on
    disk, but the integration has no live entities yet. The helper must say
    "not registered" so ``_discover()`` re-adds and HA reattaches the rows."""
    entity_already_registered, set_platforms = helper
    set_platforms()  # fresh setup: platform map is empty

    assert not entity_already_registered(
        MagicMock(), "sensor", "entry-42_target_porch.yaml_pinned"
    )


def test_platform_present_but_entity_not_yet_added(helper):
    """First ``_discover()`` of a session: the platform exists (other
    entities are live on it) but this unique_id has no owner yet."""
    entity_already_registered, set_platforms = helper
    set_platforms(_fake_platform("sensor", ["entry-42_target_garage.yaml_pinned"]))

    assert not entity_already_registered(
        MagicMock(), "sensor", "entry-42_target_porch.yaml_pinned"
    )


def test_same_unique_id_on_a_different_platform_does_not_match(helper):
    """Platform domains are matched, not ignored — an ``update`` entity must
    not suppress the ``sensor`` add that shares its unique_id shape."""
    entity_already_registered, set_platforms = helper
    set_platforms(_fake_platform("update", ["entry-42_target_porch.yaml_update"]))

    assert not entity_already_registered(
        MagicMock(), "sensor", "entry-42_target_porch.yaml_update"
    )


def test_matches_across_multiple_platforms_of_the_integration(helper):
    """The integration owns several platforms at once; the right one wins."""
    entity_already_registered, set_platforms = helper
    set_platforms(
        _fake_platform("update", ["entry-42_target_porch.yaml_update"]),
        _fake_platform("binary_sensor", ["entry-42_worker_w1_online"]),
        _fake_platform("sensor", ["entry-42_worker_w1_jobs"]),
    )

    assert entity_already_registered(MagicMock(), "binary_sensor", "entry-42_worker_w1_online")
    assert entity_already_registered(MagicMock(), "sensor", "entry-42_worker_w1_jobs")
    assert not entity_already_registered(MagicMock(), "sensor", "entry-42_worker_w2_jobs")


def test_removed_entity_becomes_addable_again(helper):
    """#62's original symptom: a worker that vanished from the snapshot and
    came back must be re-added. Dropping it from the live map is enough."""
    entity_already_registered, set_platforms = helper
    uid = "entry-42_worker_w1_online"

    set_platforms(_fake_platform("binary_sensor", [uid]))
    assert entity_already_registered(MagicMock(), "binary_sensor", uid)

    # Stale-device cleanup removed it; the live map no longer holds it.
    set_platforms(_fake_platform("binary_sensor", []))
    assert not entity_already_registered(MagicMock(), "binary_sensor", uid)


def test_does_not_consult_the_persistent_entity_registry():
    """Guard the actual #199 regression: if this module ever goes back to
    ``entity_registry.async_get``, a restart re-breaks every per-target and
    per-worker entity. The live-platform map is the only source allowed.

    Deliberately takes no fixture — the fixture above patches
    ``_discovery.entity_platform``, which only the fixed module has, so a
    fixtured test would error rather than fail on a reverted implementation.
    This one runs against either and fails cleanly on the old one."""
    from esphome_fleet import _discovery

    assert not hasattr(_discovery, "er"), (
        "_discovery must not import homeassistant.helpers.entity_registry — "
        "it persists across restarts and reintroduces #199"
    )
    assert hasattr(_discovery, "entity_platform"), (
        "_discovery must resolve liveness through the session-scoped "
        "entity_platform map (#199)"
    )
