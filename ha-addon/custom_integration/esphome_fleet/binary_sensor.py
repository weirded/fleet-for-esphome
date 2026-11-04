"""Binary sensor entities (HI.5) — worker connectivity.

One `BinarySensor` per build worker. `is_on` maps to `worker.online` (as
reported by the server's registry heartbeat check) and uses HA's
`connectivity` device class so the UI picks up the right icon + label.

Hub device: `EsphomeUpdateAvailableBinarySensor` (#257) — on when a
stable ESPHome release newer than Fleet's selected version is on PyPI.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from awesomeversion import AwesomeVersion
from awesomeversion.exceptions import AwesomeVersionException
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._discovery import entity_already_registered
from .const import DOMAIN
from .coordinator import EsphomeFleetCoordinator
from .device import hub_device_info, worker_device_info



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

    # Hub-level — always present.
    async_add_entities([
        EsphomeUpdateAvailableBinarySensor(
            coordinator, entry.entry_id, coordinator.base_url,
        ),
    ])

    def _discover() -> None:
        # #62: registry-backed check; see sensor.py::async_setup_entry.
        new: list[BinarySensorEntity] = []
        for w in (coordinator.data or {}).get("workers") or []:
            client_id = w.get("client_id")
            if not client_id:
                continue
            online = WorkerOnlineBinarySensor(coordinator, entry.entry_id, client_id)
            if not entity_already_registered(hass, "binary_sensor", online.unique_id):
                new.append(online)
            # #151 (KriVaTri): per-worker "Working" sensor so users can
            # automate add-on stop/start (VSCode, Frigate, NodeRED, …)
            # while compiles are in flight without hard-coding a target
            # add-on inside Fleet.
            working = WorkerWorkingBinarySensor(coordinator, entry.entry_id, client_id)
            if not entity_already_registered(hass, "binary_sensor", working.unique_id):
                new.append(working)
        if new:
            async_add_entities(new)

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))


class _WorkerBinarySensorBase(
    CoordinatorEntity[EsphomeFleetCoordinator], BinarySensorEntity
):
    """Shared plumbing: device-info wiring + coordinator-snapshot lookup."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._client_id = client_id

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


class WorkerOnlineBinarySensor(_WorkerBinarySensorBase):
    # CR.7: promote worker-online to a primary state sensor. Users
    # build automations like "when all build workers are offline, alert
    # me"; DIAGNOSTIC hid it from the default entity picker.
    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator, entry_id, client_id)
        self._attr_unique_id = f"{entry_id}_worker_{client_id}_online"

    @property
    def is_on(self) -> bool:
        w = self._worker or {}
        return bool(w.get("online"))


class WorkerWorkingBinarySensor(_WorkerBinarySensorBase):
    """#151: per-worker "Working" binary_sensor.

    `is_on` is True whenever the server reports at least one job in
    `WORKING` state assigned to this worker's `client_id`. Lets HA
    users wire automations around fleet activity without Fleet
    hard-coding any specific stop/start action — the canonical use
    case is "stop the VSCode add-on while a worker is busy compiling
    so the box doesn't OOM" (KriVaTri's report), but the same shape
    serves Frigate, NodeRED, AdGuard, or notification automations.

    `BinarySensorDeviceClass.RUNNING` gives HA a translated entity
    name + correct icon without needing a QS.G5 entity-translations
    key (device-class names are HA-side translations).
    """

    _attr_translation_key = "working"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator, entry_id, client_id)
        self._attr_unique_id = f"{entry_id}_worker_{client_id}_working"

    @property
    def is_on(self) -> bool:
        w = self._worker or {}
        # Prefer the explicit boolean; fall back to active_job_count > 0
        # so older server builds (pre-1.7.2-dev.6) still expose the right
        # state once the integration upgrades ahead of the add-on.
        if "is_working" in w:
            return bool(w.get("is_working"))
        return int(w.get("active_job_count") or 0) > 0


class EsphomeUpdateAvailableBinarySensor(
    CoordinatorEntity[EsphomeFleetCoordinator], BinarySensorEntity
):
    """#257: on when PyPI has a stable ESPHome newer than the selected one.

    Compares the add-on's `latest_stable` (newest non-pre-release on
    PyPI) against `selected` (the version new compiles use). Turns off
    again once the user switches Fleet to the new release. A beta
    selected ahead of the latest stable correctly reads as "off".
    Unknown (None) while either side is missing or unparsable.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "esphome_update_available"
    _attr_device_class = BinarySensorDeviceClass.UPDATE

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._base_url = base_url
        self._attr_unique_id = f"{entry_id}_esphome_update_available"

    @property
    def device_info(self):
        return hub_device_info(self._entry_id, self._base_url)

    @property
    def _versions(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("esphome_versions") or {}

    @property
    def is_on(self) -> bool | None:
        latest = self._versions.get("latest_stable")
        selected = self._versions.get("selected")
        if not latest or not selected or selected == "unknown":
            return None
        try:
            return AwesomeVersion(latest) > AwesomeVersion(selected)
        except AwesomeVersionException:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "latest_version": self._versions.get("latest_stable"),
            "selected_version": self._versions.get("selected"),
        }
