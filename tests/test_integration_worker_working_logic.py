"""Bug #151 (1.7.2): WorkerWorkingBinarySensor logic tests.

Pure-logic coverage of the per-worker "Working" binary sensor — exercises
the coordinator-snapshot lookup, the ``is_on`` derivation, and the
back-compat fall-through to ``active_job_count`` when the server hasn't
been upgraded past 1.7.2-dev.6 yet.

Mock-only (no real HA fixture required), per the ``*_logic.py`` pattern
in the rest of the integration test suite — the real-HA lifecycle is
already covered by ``test_integration_setup.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_sensor(worker: dict | None) -> "object":
    """Construct a WorkerWorkingBinarySensor against a mock coordinator
    whose ``data["workers"]`` is the single-row list ``[worker]`` (or
    empty when ``worker is None``).
    """
    from esphome_fleet.binary_sensor import WorkerWorkingBinarySensor

    coordinator = MagicMock()
    coordinator.data = {"workers": [worker] if worker is not None else []}
    return WorkerWorkingBinarySensor(coordinator, "entry-x", "client-1")


def test_is_on_true_when_is_working_flag_set() -> None:
    sensor = _make_sensor({"client_id": "client-1", "is_working": True})
    assert sensor.is_on is True


def test_is_on_false_when_is_working_flag_clear() -> None:
    sensor = _make_sensor({"client_id": "client-1", "is_working": False})
    assert sensor.is_on is False


def test_is_on_falls_back_to_active_job_count_when_flag_missing() -> None:
    """Back-compat path: integration upgraded ahead of the add-on, server
    payload omits ``is_working`` but still carries ``active_job_count``."""
    sensor = _make_sensor({"client_id": "client-1", "active_job_count": 2})
    assert sensor.is_on is True


def test_is_on_false_when_neither_field_present() -> None:
    """Pre-#151 server: neither field exists. Sensor reports False rather
    than crash with KeyError."""
    sensor = _make_sensor({"client_id": "client-1"})
    assert sensor.is_on is False


def test_is_on_false_when_worker_row_missing_from_snapshot() -> None:
    """Coordinator data still loading, or the worker was removed from the
    registry between polls. Should not raise; available should also be
    False so HA doesn't trust the stale value."""
    sensor = _make_sensor(None)
    assert sensor.is_on is False
    assert sensor.available is False


def test_unique_id_distinguishes_working_from_online() -> None:
    """The "_working" suffix on the unique_id keeps the entity distinct
    from the existing WorkerOnlineBinarySensor (which uses "_online")."""
    from esphome_fleet.binary_sensor import (
        WorkerOnlineBinarySensor,
        WorkerWorkingBinarySensor,
    )

    coord = MagicMock()
    coord.data = {"workers": [{"client_id": "client-1"}]}
    online = WorkerOnlineBinarySensor(coord, "entry-x", "client-1")
    working = WorkerWorkingBinarySensor(coord, "entry-x", "client-1")

    assert online.unique_id != working.unique_id
    assert online.unique_id.endswith("_online")
    assert working.unique_id.endswith("_working")


def test_device_class_is_running() -> None:
    """HA picks the icon + translated entity name from the device class —
    BinarySensorDeviceClass.RUNNING gives the right semantic without
    needing a custom entity-translations key."""
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

    sensor = _make_sensor({"client_id": "client-1"})
    assert sensor.device_class == BinarySensorDeviceClass.RUNNING
