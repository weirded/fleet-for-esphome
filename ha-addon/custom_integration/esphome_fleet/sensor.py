"""Sensor entities (HI.4).

Device-scoped sensors, grouped by what user-facing question they answer:

Global (hub device):
    QueueDepthSensor             — count of pending + working jobs.
    LatestEsphomeVersionSensor   — newest stable ESPHome on PyPI (#257).

Per managed target:
    TargetScheduleSensor         — human-readable recurring cron schedule
                                    (#28, #40).
    TargetScheduledOnceSensor    — one-time scheduled upgrade datetime
                                    (#40).
    TargetPinnedVersionSensor    — ESPHome version the next compile
                                    will use for this target, or "None".

Per build worker:
    WorkerActiveJobsSensor       — count of WORKING queue entries
                                    assigned to this worker.
    WorkerDiskUsageSensor        — disk utilization % (#29).
    WorkerCpuUsageSensor         — CPU utilization % (#29).
    WorkerDiskFreeSensor         — free disk space, pre-formatted (#29).
    WorkerCpuCoresSensor         — CPU core count (#29).
    WorkerMemorySensor           — total memory, pre-formatted (#29).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._discovery import entity_already_registered
from .const import DOMAIN
from .coordinator import EsphomeFleetCoordinator
from .device import hub_device_info, target_device_info, worker_device_info



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

    # Hub-level sensors — always present.
    eid = entry.entry_id
    url = coordinator.base_url
    async_add_entities([
        QueueDepthSensor(coordinator, eid, url),
        WorkerCountSensor(coordinator, eid, url),
        TotalSlotsSensor(coordinator, eid, url),
        SelectedEsphomeVersionSensor(coordinator, eid, url),
        LatestEsphomeVersionSensor(coordinator, eid, url),
        FleetVersionSensor(coordinator, eid, url),
        TotalDevicesSensor(coordinator, eid, url),
        OnlineDevicesSensor(coordinator, eid, url),
        OutdatedDevicesSensor(coordinator, eid, url),
    ])

    def _discover() -> None:
        # #62: switched from closure-scoped `seen_*` sets to an HA
        # entity-registry lookup. The old in-memory set went stale when
        # #39 cleaned up a worker/target whose coordinator snapshot had
        # briefly gone silent — the stale set then kept us from
        # recreating the entities once the worker came back. The
        # registry-backed check is self-healing across add/remove cycles.
        new: list[SensorEntity] = []
        eid = entry.entry_id

        def _needs(cls, *args) -> None:
            ent = cls(coordinator, eid, *args)
            if not entity_already_registered(hass, "sensor", ent.unique_id):
                new.append(ent)

        for t in (coordinator.data or {}).get("targets") or []:
            filename = t.get("target")
            if filename:
                _needs(TargetScheduleSensor, filename)
                _needs(TargetScheduledOnceSensor, filename)
                _needs(TargetPinnedVersionSensor, filename)

        for w in (coordinator.data or {}).get("workers") or []:
            client_id = w.get("client_id")
            if client_id:
                _needs(WorkerActiveJobsSensor, client_id)
                _needs(WorkerDiskUsageSensor, client_id)
                _needs(WorkerCpuUsageSensor, client_id)
                _needs(WorkerDiskFreeSensor, client_id)
                _needs(WorkerCpuCoresSensor, client_id)
                _needs(WorkerMemorySensor, client_id)

        if new:
            async_add_entities(new)

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))


class QueueDepthSensor(CoordinatorEntity[EsphomeFleetCoordinator], SensorEntity):
    """Count of non-terminal (pending + working) jobs."""

    _attr_has_entity_name = True
    _attr_translation_key = "queue_depth"
    _attr_icon = "mdi:tray-full"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "jobs"
    # CR.7: this is a primary state sensor (users dashboard on it,
    # trigger automations off it). DIAGNOSTIC hid it from the default
    # Lovelace picker + entity-picker filters.

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._base_url = base_url
        self._attr_unique_id = f"{entry_id}_queue_depth"

    @property
    def device_info(self):
        return hub_device_info(self._entry_id, self._base_url)

    @property
    def native_value(self) -> int:
        queue = (self.coordinator.data or {}).get("queue") or []
        return sum(1 for j in queue if j.get("state") in ("pending", "working"))


class _HubSensorBase(CoordinatorEntity[EsphomeFleetCoordinator], SensorEntity):
    """Shared hub-device-scoping boilerplate.

    CR.7: entity_category is NOT set here — subclasses opt in to
    DIAGNOSTIC only when they represent genuinely diagnostic info
    (version strings, build ids). State sensors (`WorkerCountSensor`,
    `TotalSlotsSensor`, `TotalDevicesSensor`, `OnlineDevicesSensor`,
    `OutdatedDevicesSensor`) are primary: users dashboard on them and
    trigger automations off them.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EsphomeFleetCoordinator,
        entry_id: str,
        base_url: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._base_url = base_url
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"

    @property
    def device_info(self):
        return hub_device_info(self._entry_id, self._base_url)


class WorkerCountSensor(_HubSensorBase):
    """#42 — number of registered workers."""

    _attr_translation_key = "workers"
    _attr_icon = "mdi:server-network"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "workers"

    def __init__(self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str) -> None:
        super().__init__(coordinator, entry_id, base_url, "worker_count")

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("workers") or [])


class TotalSlotsSensor(_HubSensorBase):
    """#42 — sum of max_parallel_jobs across all workers."""

    _attr_translation_key = "total_build_slots"
    _attr_icon = "mdi:slot-machine"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "slots"

    def __init__(self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str) -> None:
        super().__init__(coordinator, entry_id, base_url, "total_slots")

    @property
    def native_value(self) -> int:
        workers = (self.coordinator.data or {}).get("workers") or []
        return sum(w.get("max_parallel_jobs", 0) for w in workers)


class SelectedEsphomeVersionSensor(_HubSensorBase):
    """#43 — currently selected ESPHome version on the server."""

    _attr_translation_key = "selected_esphome_version"
    _attr_icon = "mdi:tag"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str) -> None:
        super().__init__(coordinator, entry_id, base_url, "selected_esphome_version")

    @property
    def native_value(self) -> str | None:
        versions = (self.coordinator.data or {}).get("esphome_versions") or {}
        return versions.get("selected") or None


class LatestEsphomeVersionSensor(_HubSensorBase):
    """#257 — newest stable ESPHome release on PyPI, as known to the add-on.

    The add-on refreshes the PyPI list hourly. State changes exactly when
    a new ESPHome release ships, so a `state` trigger on this entity is
    the "notify me about new ESPHome releases" automation. Not
    DIAGNOSTIC: users trigger automations off it. `None` (unknown) while
    PyPI hasn't been reached yet.
    """

    _attr_translation_key = "latest_esphome_version"
    _attr_icon = "mdi:tag-arrow-up"

    def __init__(self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str) -> None:
        super().__init__(coordinator, entry_id, base_url, "latest_esphome_version")

    @property
    def native_value(self) -> str | None:
        versions = (self.coordinator.data or {}).get("esphome_versions") or {}
        return versions.get("latest_stable") or None


class FleetVersionSensor(_HubSensorBase):
    """#44 — Fleet for ESPHome add-on version."""

    _attr_translation_key = "fleet_version"
    _attr_icon = "mdi:information-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str) -> None:
        super().__init__(coordinator, entry_id, base_url, "fleet_version")

    @property
    def native_value(self) -> str | None:
        info = (self.coordinator.data or {}).get("server_info") or {}
        return info.get("addon_version") or None


class TotalDevicesSensor(_HubSensorBase):
    """#46 — count of managed targets."""

    _attr_translation_key = "total_devices"
    _attr_icon = "mdi:devices"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str) -> None:
        super().__init__(coordinator, entry_id, base_url, "total_devices")

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("targets") or [])


class OnlineDevicesSensor(_HubSensorBase):
    """#46 — count of online targets."""

    _attr_translation_key = "online_devices"
    _attr_icon = "mdi:lan-connect"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str) -> None:
        super().__init__(coordinator, entry_id, base_url, "online_devices")

    @property
    def native_value(self) -> int:
        targets = (self.coordinator.data or {}).get("targets") or []
        return sum(1 for t in targets if t.get("online"))


class OutdatedDevicesSensor(_HubSensorBase):
    """#46 — count of targets needing an update."""

    _attr_translation_key = "outdated_devices"
    _attr_icon = "mdi:update"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, coordinator: EsphomeFleetCoordinator, entry_id: str, base_url: str) -> None:
        super().__init__(coordinator, entry_id, base_url, "outdated_devices")

    @property
    def native_value(self) -> int:
        targets = (self.coordinator.data or {}).get("targets") or []
        return sum(1 for t in targets if t.get("needs_update"))


_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _format_cron_human(cron: str) -> str:
    """Translate a 5-field cron expression to a human-readable string.

    Port of the UI's ``formatCronHuman`` (``utils/cron.ts``). Falls back
    to the raw expression for patterns it doesn't recognize.
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, _mon, dow = parts

    if minute == "0" and hour.startswith("*/"):
        try:
            n = int(hour[2:])
        except ValueError:
            return cron
        return "Hourly" if n == 1 else f"Every {n}h"

    if dom == "*" and dow == "*" and "/" not in hour and "/" not in minute:
        try:
            h, m = int(hour), int(minute)
        except ValueError:
            return cron
        return f"Daily {h:02d}:{m:02d}"

    if dom == "*" and dow != "*" and "/" not in hour:
        try:
            h, m, d = int(hour), int(minute), int(dow)
        except ValueError:
            return cron
        day = _DAY_NAMES[d] if 0 <= d < 7 else dow
        return f"{day} {h:02d}:{m:02d}"

    if dom != "*" and dow == "*" and "/" not in hour:
        try:
            h, m = int(hour), int(minute)
        except ValueError:
            return cron
        suffix = "st" if dom == "1" else "nd" if dom == "2" else "rd" if dom == "3" else "th"
        return f"{dom}{suffix} {h:02d}:{m:02d}"

    return cron


class _TargetSensorBase(CoordinatorEntity[EsphomeFleetCoordinator], SensorEntity):
    """Shared target-scoping boilerplate."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EsphomeFleetCoordinator,
        entry_id: str,
        target_filename: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._target_filename = target_filename
        self._attr_unique_id = f"{entry_id}_target_{target_filename}_{unique_suffix}"

    @property
    def _target(self) -> dict[str, Any] | None:
        for t in (self.coordinator.data or {}).get("targets") or []:
            if t.get("target") == self._target_filename:
                return t
        return None

    @property
    def available(self) -> bool:
        return super().available and self._target is not None

    @property
    def device_info(self):
        t = self._target or {"target": self._target_filename}
        return target_device_info(t, self._entry_id)


class TargetScheduleSensor(_TargetSensorBase):
    """Human-readable schedule for the next automatic compile (#28, #34).

    Exposed to HA as "Upgrade schedule" to make clear this is the cron
    / one-shot that drives automatic compile + OTA upgrades — not the
    device's own internal schedule.
    """

    _attr_translation_key = "upgrade_schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, target_filename: str
    ) -> None:
        super().__init__(coordinator, entry_id, target_filename, "schedule")

    @property
    def native_value(self) -> str | None:
        t = self._target or {}
        cron = t.get("schedule")
        enabled = t.get("schedule_enabled")
        if cron and enabled:
            human = _format_cron_human(cron)
            tz = t.get("schedule_tz")
            return f"{human} ({tz})" if tz else human
        if cron and not enabled:
            return "Paused"
        return "None"


class TargetScheduledOnceSensor(_TargetSensorBase):
    """One-time scheduled upgrade datetime (#40).

    Separate from the recurring schedule so dashboards can distinguish
    "next scheduled OTA" from "ongoing cron". Reports the ISO datetime
    in a human-friendly format, or "None" when no one-shot is pending.
    """

    _attr_translation_key = "scheduled_one_time_upgrade"
    _attr_icon = "mdi:calendar-star"

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, target_filename: str
    ) -> None:
        super().__init__(coordinator, entry_id, target_filename, "schedule_once")

    @property
    def native_value(self) -> str:
        t = self._target or {}
        once = t.get("schedule_once")
        if not once:
            return "None"
        # schedule_once is an ISO datetime string from the server.
        # Parse and render in a friendlier format.
        try:
            from datetime import datetime  # noqa: PLC0415
            dt = datetime.fromisoformat(once)
            return dt.strftime("%b %d, %Y %H:%M")
        except (ValueError, TypeError):
            return str(once)


class TargetPinnedVersionSensor(_TargetSensorBase):
    """ESPHome version pin for this target (#28).

    Reports the pinned version string, or "None" (#35) when no pin is
    set. Useful for dashboards that want to flag targets stuck on old
    releases.
    """

    _attr_translation_key = "pinned_esphome_version"
    _attr_icon = "mdi:pin"

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, target_filename: str
    ) -> None:
        super().__init__(coordinator, entry_id, target_filename, "pinned_version")

    @property
    def native_value(self) -> str:
        t = self._target or {}
        return str(t.get("pinned_version") or "None")


class _WorkerSensorBase(CoordinatorEntity[EsphomeFleetCoordinator], SensorEntity):
    """Shared worker-scoping boilerplate."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EsphomeFleetCoordinator,
        entry_id: str,
        client_id: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._client_id = client_id
        self._attr_unique_id = f"{entry_id}_worker_{client_id}_{unique_suffix}"

    @property
    def _worker(self) -> dict[str, Any] | None:
        for w in (self.coordinator.data or {}).get("workers") or []:
            if w.get("client_id") == self._client_id:
                return w
        return None

    @property
    def _system_info(self) -> dict[str, Any]:
        return (self._worker or {}).get("system_info") or {}

    @property
    def available(self) -> bool:
        return super().available and self._worker is not None

    @property
    def device_info(self):
        w = self._worker or {"client_id": self._client_id}
        return worker_device_info(w, self._entry_id)


class WorkerActiveJobsSensor(_WorkerSensorBase):
    """Count of WORKING jobs assigned to the worker."""

    _attr_translation_key = "active_jobs"
    _attr_icon = "mdi:wrench-cog"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "jobs"

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator, entry_id, client_id, "active_jobs")

    @property
    def native_value(self) -> int:
        queue = (self.coordinator.data or {}).get("queue") or []
        return sum(
            1
            for j in queue
            if j.get("state") == "working"
            and j.get("assigned_client_id") == self._client_id
        )


class WorkerDiskUsageSensor(_WorkerSensorBase):
    """Disk utilization % reported by the worker (#29)."""

    _attr_translation_key = "disk_usage"
    _attr_icon = "mdi:harddisk"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator, entry_id, client_id, "disk_usage")

    @property
    def native_value(self) -> float | None:
        pct = self._system_info.get("disk_used_pct")
        try:
            return float(pct) if pct is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class WorkerCpuCoresSensor(_WorkerSensorBase):
    """CPU core count reported by the worker (#29)."""

    _attr_translation_key = "cpu_cores"
    _attr_icon = "mdi:cpu-64-bit"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "cores"

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator, entry_id, client_id, "cpu_cores")

    @property
    def native_value(self) -> int | None:
        cores = self._system_info.get("cpu_cores")
        try:
            return int(cores) if cores is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class WorkerCpuUsageSensor(_WorkerSensorBase):
    """CPU utilization % reported by the worker (#29)."""

    _attr_translation_key = "cpu_usage"
    _attr_icon = "mdi:gauge"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator, entry_id, client_id, "cpu_usage")

    @property
    def native_value(self) -> float | None:
        pct = self._system_info.get("cpu_usage")
        try:
            return float(pct) if pct is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class WorkerDiskFreeSensor(_WorkerSensorBase):
    """Free disk space on the worker's build volume (#29).

    The client ships this as a pre-formatted string (e.g. ``"350 GB"``),
    so we expose it as a plain text sensor — no unit, no state class.
    Matches what the UI's Workers tab shows.
    """

    _attr_translation_key = "disk_free"
    _attr_icon = "mdi:harddisk-plus"

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator, entry_id, client_id, "disk_free")

    @property
    def native_value(self) -> str | None:
        value = self._system_info.get("disk_free")
        return str(value) if value else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class WorkerMemorySensor(_WorkerSensorBase):
    """Total memory reported by the worker (#29).

    Pre-formatted string sensor — see :class:`WorkerDiskFreeSensor`.
    """

    _attr_translation_key = "memory"
    _attr_icon = "mdi:memory"

    def __init__(
        self, coordinator: EsphomeFleetCoordinator, entry_id: str, client_id: str
    ) -> None:
        super().__init__(coordinator, entry_id, client_id, "memory")

    @property
    def native_value(self) -> str | None:
        value = self._system_info.get("total_memory")
        return str(value) if value else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None
