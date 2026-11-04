"""Shared helper: has this integration already got a live entity for a unique_id?

Why this matters (#62): each platform's ``async_setup_entry`` used to
keep an in-memory ``seen_workers`` / ``seen_targets`` set to avoid
double-adding entities on coordinator updates. That worked fine on
first setup, but broke whenever the stale-device cleanup (#39) removed
a worker or target that had briefly vanished from the coordinator
snapshot — the closure's ``seen_*`` set still contained that
``client_id`` / filename, so ``_discover`` saw it on the next refresh
and skipped recreation. Result: device re-appeared in HA's registry
but with zero entities (visible in #62 on hass-4 where 6 of 7 worker
devices had no entities after the SE workstream restart churn).

Why the #62 fix had to change (#199): the replacement asked HA's
**entity registry** whether the unique_id was known. That registry is
persisted to disk and survives restarts, while the ``CoordinatorEntity``
instances it points at are session-scoped — rebuilt by
``async_setup_entry``, dropped on unload. So on the first tick after
any HA restart or integration reload, the registry answered "yes,
seen it" for *every* per-target and per-worker unique_id, ``_discover``
skipped all of them, and the registry rows were left owned by nothing.
HA renders exactly that state as ``unavailable`` with *"This entity is
no longer being provided by the esphome_fleet integration."* Only
cluster-level sensors — added unconditionally rather than through
``_discover`` — survived a restart, which is what made the report so
specific about *per-target and per-worker* entities. Diagnosed and
patched by the reporter of #199 after tracing it through this module.

Fix: ask the **live** entity-platform map instead. It is rebuilt from
scratch every time the integration sets up, so after a restart the
answer is "no" and ``_discover`` re-adds; HA then reattaches those
entities to their existing registry rows by unique_id, preserving
entity_ids, names, areas, and history. Within a single session the
answer flips to "yes" as soon as an entity is live, so the duplicate
adds #62 was about are still prevented — and so is the add/remove
cycle #62 regressed on, because a removed entity leaves the live map
too.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform

from .const import DOMAIN


def entity_already_registered(
    hass: HomeAssistant, platform: str, unique_id: str
) -> bool:
    """Return True if a *live* entity with *unique_id* exists on *platform*.

    *platform* is the entity domain (``"sensor"``, ``"update"``, …), matched
    against ``EntityPlatform.domain``. Unique_ids are config-entry-scoped
    (``f"{entry_id}_target_…"``), so scanning every platform this integration
    owns cannot collide across entries.
    """
    for plat in entity_platform.async_get_platforms(hass, DOMAIN):
        if plat.domain != platform:
            continue
        for ent in plat.entities.values():
            if ent.unique_id == unique_id:
                return True
    return False
