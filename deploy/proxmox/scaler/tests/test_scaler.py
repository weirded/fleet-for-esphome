"""Unit tests for the reconciliation loop. Fleet + Proxmox clients are mocked."""

from __future__ import annotations

from dataclasses import dataclass

from esphome_fleet_proxmox_scaler.config import Config
from esphome_fleet_proxmox_scaler.scaler import Scaler


@dataclass
class _FakeStatus:
    queue_size: int
    online_workers: int = 0


class _FakeFleet:
    def __init__(self, queue_size: int) -> None:
        self.queue_size = queue_size
        self.calls = 0

    def status(self) -> _FakeStatus:
        self.calls += 1
        return _FakeStatus(queue_size=self.queue_size)


class _FakeProxmox:
    def __init__(self, running: list[int], stopped: list[int]) -> None:
        self._running = list(running)
        self._stopped = list(stopped)
        self.started: list[int] = []
        self.stopped: list[int] = []

    def running_vmids(self, vmids):
        return [v for v in self._running if v in vmids]

    def stopped_vmids(self, vmids):
        return [v for v in self._stopped if v in vmids]

    def start(self, vmid):
        self._stopped.remove(vmid)
        self._running.append(vmid)
        self.started.append(vmid)

    def stop(self, vmid):
        self._running.remove(vmid)
        self._stopped.append(vmid)
        self.stopped.append(vmid)


def _cfg(**overrides) -> Config:
    base = dict(
        fleet_url="http://example",
        fleet_token="t",
        proxmox_host="px",
        proxmox_verify_ssl=True,
        proxmox_token_id="root@pam!s",
        proxmox_token_secret="x",
        proxmox_node="pve",
        vmids=(200, 201, 202, 203, 204),
        min_workers=1,
        max_workers=3,
        target_per_worker=2,
        poll_interval=30,
        cooldown_seconds=600,
        log_level="INFO",
    )
    base.update(overrides)
    return Config(**base)


# desired_count


def test_desired_count_zero_queue_returns_min_workers():
    s = Scaler(_cfg(min_workers=1), _FakeFleet(0), _FakeProxmox([], [200]))
    assert s.desired_count(0) == 1


def test_desired_count_zero_queue_zero_min():
    s = Scaler(_cfg(min_workers=0), _FakeFleet(0), _FakeProxmox([], [200]))
    assert s.desired_count(0) == 0


def test_desired_count_uses_ceil():
    # queue_size=3, target=2 → ceil(3/2)=2
    s = Scaler(_cfg(target_per_worker=2), _FakeFleet(3), _FakeProxmox([], [200]))
    assert s.desired_count(3) == 2


def test_desired_count_clamped_to_max():
    # queue_size=100, target=2 → 50, but max=3
    s = Scaler(_cfg(max_workers=3), _FakeFleet(100), _FakeProxmox([], [200]))
    assert s.desired_count(100) == 3


def test_desired_count_clamped_to_min():
    # queue_size=1 with min_workers=2 → still 2
    s = Scaler(_cfg(min_workers=2, target_per_worker=2), _FakeFleet(1), _FakeProxmox([], [200, 201]))
    assert s.desired_count(1) == 2


# tick — scale up


def test_scale_up_starts_lowest_vmid_first():
    fleet = _FakeFleet(queue_size=4)  # desired = 2 with target_per_worker=2
    proxmox = _FakeProxmox(running=[], stopped=[200, 201, 202])
    s = Scaler(_cfg(min_workers=0, max_workers=3), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "scale_up"
    assert out["started"] == [200, 201]
    assert proxmox.started == [200, 201]


def test_scale_up_blocked_when_pool_exhausted():
    fleet = _FakeFleet(queue_size=10)
    proxmox = _FakeProxmox(running=[200, 201, 202], stopped=[])
    s = Scaler(_cfg(min_workers=0, max_workers=5), fleet, proxmox)
    # desired = ceil(10/2) = 5, current = 3 → want +2 but pool empty
    out = s.tick()
    assert out["action"] == "scale_up_blocked"
    assert out["wanted"] == 2
    assert out["available"] == 0


# tick — scale down


def test_scale_down_stops_highest_vmid_first():
    fleet = _FakeFleet(queue_size=0)
    proxmox = _FakeProxmox(running=[200, 201, 202], stopped=[])
    s = Scaler(_cfg(min_workers=1, cooldown_seconds=0), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "scale_down"
    # min=1 → keep 200, stop 201 and 202 (highest first)
    assert out["stopped"] == [202, 201]


def test_scale_down_cooldown_blocks():
    """After a scale-down, a subsequent scale-down attempt within the cooldown
    window must be blocked even if there are still running pods to drop.

    Simulating that "still has things to scale down" requires putting pods
    back externally between ticks (as if an operator manually started one,
    or an earlier scale-up just landed and the queue then dropped to 0).
    """
    fleet = _FakeFleet(queue_size=0)
    proxmox = _FakeProxmox(running=[200, 201, 202], stopped=[])
    clock = iter([0.0, 10.0])
    s = Scaler(
        _cfg(min_workers=1, cooldown_seconds=300),
        fleet,
        proxmox,
        clock=lambda: next(clock),
    )
    out1 = s.tick()
    assert out1["action"] == "scale_down"

    # Operator (or some other actor) starts an extra LXC manually.
    proxmox._running.extend([201, 202])  # noqa: SLF001 — test fixture
    proxmox._stopped = [v for v in proxmox._stopped if v not in (201, 202)]  # noqa: SLF001

    out2 = s.tick()
    assert out2["action"] == "scale_down_cooldown"
    assert "remaining" in out2


def test_scale_down_cooldown_does_not_block_first_call():
    """A fresh process (no prior scale-down) must be able to scale down on tick #1."""
    fleet = _FakeFleet(queue_size=0)
    proxmox = _FakeProxmox(running=[200, 201], stopped=[])
    s = Scaler(_cfg(min_workers=1, cooldown_seconds=999_999), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "scale_down"


# tick — noop


def test_tick_noop_when_at_desired_count():
    # queue_size=2, target=2 → desired=1; min=1; running already 1
    fleet = _FakeFleet(queue_size=2)
    proxmox = _FakeProxmox(running=[200], stopped=[201, 202])
    s = Scaler(_cfg(min_workers=1, max_workers=3, target_per_worker=2), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "noop"
    assert out["running"] == 1
    assert out["desired"] == 1


def test_tick_skips_on_fleet_unreachable():
    class _ErrFleet:
        def status(self):
            raise RuntimeError("kaboom")
    proxmox = _FakeProxmox(running=[200], stopped=[201])
    s = Scaler(_cfg(), _ErrFleet(), proxmox)
    out = s.tick()
    assert out["action"] == "skip"
    assert out["reason"] == "fleet_unreachable"
    # Crucially: no Proxmox writes happen if we couldn't read the queue.
    assert proxmox.started == []
    assert proxmox.stopped == []
