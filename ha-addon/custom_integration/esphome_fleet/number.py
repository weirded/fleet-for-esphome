"""Number entities — worker parallel-jobs slot count (#47).

One NumberEntity per build worker. Setting the value POSTs to the
add-on's ``/ui/api/workers/{client_id}/parallel-jobs`` endpoint, which
updates the worker's ``requested_max_parallel_jobs``; the worker picks
it up on its next heartbeat. Setting to 0 effectively pauses the worker.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._discovery import entity_already_registered
from .const import DOMAIN
from .coordinator import EsphomeFleetCoordinator
from .device import worker_device_info



# Silver quality-scale: parallel-updates rule. Coordinator-driven
# local-polling integration — the single EsphomeFleetCoordinator
# owns polling and hands all entities the same snapshot, so HA's
# per-platform serializer just adds startup latency. Setting to 0
# tells HA this platform does its own concurrency control.
PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EsphomeFleetCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _discover() -> None:
        # #62: registry-backed check; see sensor.py::async_setup_entry.
        new: list[WorkerSlotCountNumber] = []
        for w in (coordinator.data or {}).get("workers") or []:
            client_id = w.get("client_id")
            if not client_id:
                continue
            ent = WorkerSlotCountNumber(coordinator, entry.entry_id, client_id)
            if not entity_already_registered(hass, "number", ent.unique_id):
                new.append(ent)
        if new:
            async_add_entities(new)

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))


class WorkerSlotCountNumber(
    CoordinatorEntity[EsphomeFleetCoordinator], NumberEntity
):
    _attr_has_entity_name = True
    _attr_translation_key = "build_slots"
    _attr_icon = "mdi:slot-machine"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 32
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: EsphomeFleetCoordinator,
        entry_id: str,
        client_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._client_id = client_id
        self._attr_unique_id = f"{entry_id}_worker_{client_id}_slots"

    @property
    def _worker(self) -> dict[str, Any] | None:
        for w in (self.coordinator.data or {}).get("workers") or []:
            if w.get("client_id") == self._client_id:
                return w
        return None

    @property
    def available(self) -> bool:
        return super().available and self._worker is not None

    @property
    def device_info(self):
        w = self._worker or {"client_id": self._client_id}
        return worker_device_info(w, self._entry_id)

    @property
    def native_value(self) -> float | None:
        w = self._worker
        if w is None:
            return None
        return float(w.get("max_parallel_jobs", 0))

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_post_json(
            f"/ui/api/workers/{self._client_id}/parallel-jobs",
            {"max_parallel_jobs": int(value)},
        )
        await self.coordinator.async_request_refresh()
