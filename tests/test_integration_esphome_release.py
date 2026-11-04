"""#257 — real-hass tests for the "new ESPHome release" hub entities.

``sensor.*_latest_esphome_version`` mirrors the add-on's ``latest_stable``
(newest non-pre-release on PyPI); ``binary_sensor.*_esphome_update_available``
is on when that's newer than Fleet's selected version. Drives a real
``hass`` through setup → coordinator refresh so the state transitions
users trigger automations on are pinned end to end.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, patch

# PY-10: real-hass plugin import keeps the non-_logic filename honest.
import pytest_homeassistant_custom_component  # noqa: F401

from pytest_homeassistant_custom_component.common import MockConfigEntry

from _integration_test_fixtures import (  # noqa: F401 — autouse fixtures
    _install_integration_in_hass_config,
    _warm_pycares_shutdown_thread,
)


def _snapshot(selected: str | None, latest: str | None) -> dict[str, Any]:
    return {
        "server_info": {"addon_version": "1.8.0-test"},
        "targets": [],
        "devices": [],
        "workers": [],
        "queue": [],
        "esphome_versions": {
            "selected": selected,
            "detected": selected,
            "available": [v for v in (latest, selected) if v],
            "latest_stable": latest,
        },
    }


def _entity_id(hass, domain: str, entry_id: str, suffix: str) -> str:
    from homeassistant.helpers import entity_registry as er

    eid = er.async_get(hass).async_get_entity_id(
        domain, "esphome_fleet", f"{entry_id}_{suffix}",
    )
    assert eid is not None, f"{domain} {suffix} not registered"
    return eid


async def test_latest_version_sensor_and_update_available(hass):
    from custom_components.esphome_fleet.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"base_url": "http://test-addon.local:8765", "token": "test-token"},
        title="Fleet — release test",
    )
    entry.add_to_hass(hass)

    state: dict[str, Any] = {"data": _snapshot("2026.10.2", "2026.10.2")}

    async def _fetch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return copy.deepcopy(state["data"])

    with patch(
        "custom_components.esphome_fleet.coordinator.EsphomeFleetCoordinator._async_update_data",
        new=_fetch,
    ), patch(
        "custom_components.esphome_fleet.ws_client.EventStreamClient._run",
        new_callable=AsyncMock,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        sensor_id = _entity_id(hass, "sensor", entry.entry_id, "latest_esphome_version")
        binary_id = _entity_id(hass, "binary_sensor", entry.entry_id, "esphome_update_available")
        coordinator = hass.data[DOMAIN][entry.entry_id]

        # Up to date.
        assert hass.states.get(sensor_id).state == "2026.10.2"
        assert hass.states.get(binary_id).state == "off"

        # New ESPHome release lands on PyPI.
        state["data"] = _snapshot("2026.10.2", "2026.10.3")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(sensor_id).state == "2026.10.3"
        b = hass.states.get(binary_id)
        assert b.state == "on"
        assert b.attributes["latest_version"] == "2026.10.3"
        assert b.attributes["selected_version"] == "2026.10.2"

        # User selects a beta ahead of latest stable → not "outdated".
        state["data"] = _snapshot("2026.11.0b1", "2026.10.3")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(binary_id).state == "off"

        # PyPI unreachable → unknown, not a false "up to date".
        state["data"] = _snapshot("2026.10.3", None)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(sensor_id).state == "unknown"
        assert hass.states.get(binary_id).state == "unknown"

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
