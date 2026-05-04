"""Unit tests for the multi-node reconciliation loop."""

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

    def status(self) -> _FakeStatus:
        return _FakeStatus(queue_size=self.queue_size)


@dataclass
class _Lxc:
    node: str
    vmid: int
    status: str


class _FakeProxmox:
    def __init__(self, nodes: list[str], pool: dict[str, list[_Lxc]]) -> None:
        self._nodes = nodes
        self._pool = pool
        self.started: list[tuple[str, int]] = []
        self.stopped_calls: list[tuple[str, int]] = []

    def list_online_nodes(self):
        return list(self._nodes)

    def list_workers_by_node(self, tag):
        # tag is ignored in the fake — we trust the test fixture.
        return {n: list(lxcs) for n, lxcs in self._pool.items()}

    def start(self, node, vmid):
        self.started.append((node, vmid))
        for l in self._pool.get(node, []):
            if l.vmid == vmid:
                l.status = "running"
                return
        raise AssertionError(f"start({node},{vmid}) — vmid not in pool")

    def stop(self, node, vmid):
        self.stopped_calls.append((node, vmid))
        for l in self._pool.get(node, []):
            if l.vmid == vmid:
                l.status = "stopped"
                return
        raise AssertionError(f"stop({node},{vmid}) — vmid not in pool")


def _cfg(**overrides) -> Config:
    base = dict(
        fleet_url="http://example",
        fleet_token="t",
        proxmox_host="px",
        proxmox_verify_ssl=True,
        proxmox_token_id="root@pam!s",
        proxmox_token_secret="x",
        workers_per_node=1,
        per_node_overrides={},
        min_total_workers=0,
        max_total_workers=None,
        worker_tag="esphome-fleet-worker",
        target_per_worker=2,
        poll_interval=30,
        cooldown_seconds=600,
        log_level="INFO",
    )
    base.update(overrides)
    return Config(**base)


# desired_count


def test_desired_count_zero_queue_returns_min_total():
    s = Scaler(_cfg(min_total_workers=2), _FakeFleet(0), _FakeProxmox([], {}))
    assert s.desired_count(0, max_total=10) == 2


def test_desired_count_clamped_to_max_total():
    s = Scaler(_cfg(target_per_worker=2), _FakeFleet(100), _FakeProxmox([], {}))
    assert s.desired_count(100, max_total=3) == 3


# Three-node default scenario: queue=0, want default min=0 → no workers running.


def test_three_node_default_zero_queue_noop_when_already_zero():
    pool = {n: [_Lxc(n, vmid=200, status="stopped")] for n in ["pve1", "pve2", "pve3"]}
    fleet = _FakeFleet(queue_size=0)
    proxmox = _FakeProxmox(["pve1", "pve2", "pve3"], pool)
    s = Scaler(_cfg(), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "noop"
    assert out["running"] == 0


def test_three_node_min_total_one_per_node_starts_one_each():
    """With 3 nodes, default workers_per_node=1, and min_total=3, the scaler
    should bring up exactly one LXC per node (the user's "1 LXC per node" default)."""
    pool = {n: [_Lxc(n, vmid=200, status="stopped")] for n in ["pve1", "pve2", "pve3"]}
    fleet = _FakeFleet(queue_size=0)
    proxmox = _FakeProxmox(["pve1", "pve2", "pve3"], pool)
    s = Scaler(_cfg(min_total_workers=3), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "scale_up"
    started_nodes = {n for n, _ in out["started"]}
    assert started_nodes == {"pve1", "pve2", "pve3"}
    assert proxmox.started == [("pve1", 200), ("pve2", 200), ("pve3", 200)] or \
           sorted(proxmox.started) == sorted([("pve1", 200), ("pve2", 200), ("pve3", 200)])


# Per-node overrides


def test_per_node_override_caps_specific_node():
    """pve-beast has override=2, pve-default uses default workers_per_node=1.
    Queue forces 3 workers; expect 2 on beast + 1 on default = 3 total."""
    pool = {
        "pve-beast": [
            _Lxc("pve-beast", vmid=200, status="stopped"),
            _Lxc("pve-beast", vmid=201, status="stopped"),
            _Lxc("pve-beast", vmid=202, status="stopped"),
        ],
        "pve-default": [
            _Lxc("pve-default", vmid=300, status="stopped"),
            _Lxc("pve-default", vmid=301, status="stopped"),
        ],
    }
    fleet = _FakeFleet(queue_size=6)  # ceil(6/2) = 3
    proxmox = _FakeProxmox(["pve-beast", "pve-default"], pool)
    s = Scaler(_cfg(per_node_overrides={"pve-beast": 2}), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "scale_up"
    # Total cap: pve-beast=2 + pve-default=1 = 3. Should start 3.
    assert len(out["started"]) == 3
    counts: dict[str, int] = {}
    for n, _ in out["started"]:
        counts[n] = counts.get(n, 0) + 1
    assert counts["pve-beast"] == 2
    assert counts["pve-default"] == 1


def test_per_node_override_zero_skips_node():
    """A node with override=0 should never get a worker started."""
    pool = {
        "pve-tiny": [_Lxc("pve-tiny", vmid=200, status="stopped")],
        "pve-real": [_Lxc("pve-real", vmid=300, status="stopped")],
    }
    fleet = _FakeFleet(queue_size=10)  # ceil(10/2)=5, way over capacity
    proxmox = _FakeProxmox(["pve-tiny", "pve-real"], pool)
    s = Scaler(_cfg(per_node_overrides={"pve-tiny": 0}), fleet, proxmox)
    out = s.tick()
    # Only pve-real has capacity (1) — total cap = 1.
    assert out["action"] == "scale_up"
    assert out["started"] == [("pve-real", 300)]


# Spread


def test_scale_up_spreads_across_nodes_evenly():
    """Two nodes with 2-cap each. Want 2 → expect 1 on each, not 2 on one."""
    pool = {
        "pve1": [_Lxc("pve1", vmid=200, status="stopped"),
                 _Lxc("pve1", vmid=201, status="stopped")],
        "pve2": [_Lxc("pve2", vmid=300, status="stopped"),
                 _Lxc("pve2", vmid=301, status="stopped")],
    }
    fleet = _FakeFleet(queue_size=4)  # ceil(4/2)=2
    proxmox = _FakeProxmox(["pve1", "pve2"], pool)
    s = Scaler(_cfg(workers_per_node=2), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "scale_up"
    nodes = [n for n, _ in out["started"]]
    assert sorted(nodes) == ["pve1", "pve2"]


# Scale down — picks node with most running


def test_scale_down_sheds_load_from_busiest_node():
    pool = {
        "pve1": [_Lxc("pve1", vmid=200, status="running"),
                 _Lxc("pve1", vmid=201, status="running")],
        "pve2": [_Lxc("pve2", vmid=300, status="running")],
    }
    fleet = _FakeFleet(queue_size=0)
    proxmox = _FakeProxmox(["pve1", "pve2"], pool)
    # min_total=1 → desired=1; current=3 → stop 2.
    s = Scaler(_cfg(workers_per_node=2, min_total_workers=1, cooldown_seconds=0), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "scale_down"
    # First stop: pve1 (busier with 2) — highest vmid 201.
    # Second stop: pve1 again still has 1 vs pve2 1 — alphabetical tie-break is non-spec'd, but
    # at least one stop must come from pve1 first.
    assert ("pve1", 201) in out["stopped"]


# Cooldown


def test_scale_down_cooldown_blocks_subsequent_attempt():
    pool = {
        "pve1": [_Lxc("pve1", vmid=200, status="running"),
                 _Lxc("pve1", vmid=201, status="running")],
    }
    fleet = _FakeFleet(queue_size=0)
    proxmox = _FakeProxmox(["pve1"], pool)
    clock = iter([0.0, 10.0])
    s = Scaler(
        _cfg(workers_per_node=2, min_total_workers=0, cooldown_seconds=300),
        fleet, proxmox, clock=lambda: next(clock),
    )
    out1 = s.tick()
    assert out1["action"] == "scale_down"
    # restore so there's something to scale down on tick 2
    pool["pve1"][0].status = "running"
    pool["pve1"][1].status = "running"
    proxmox._pool["pve1"] = pool["pve1"]
    out2 = s.tick()
    assert out2["action"] == "scale_down_cooldown"


# Failure handling


def test_tick_skips_on_fleet_unreachable():
    class _ErrFleet:
        def status(self):
            raise RuntimeError("kaboom")
    proxmox = _FakeProxmox(["pve1"], {"pve1": [_Lxc("pve1", 200, "stopped")]})
    s = Scaler(_cfg(min_total_workers=1), _ErrFleet(), proxmox)
    out = s.tick()
    assert out["action"] == "skip"
    assert out["reason"] == "fleet_unreachable"
    assert proxmox.started == []
    assert proxmox.stopped_calls == []


def test_tick_skips_on_proxmox_unreachable():
    class _ErrProxmox:
        def list_online_nodes(self):
            raise RuntimeError("api down")
        def list_workers_by_node(self, tag):
            return {}
        def start(self, *a, **kw): pass
        def stop(self, *a, **kw): pass
    fleet = _FakeFleet(queue_size=10)
    s = Scaler(_cfg(), fleet, _ErrProxmox())
    out = s.tick()
    assert out["action"] == "skip"
    assert out["reason"] == "proxmox_unreachable"


# Empty pool (fresh install before bootstrap)


def test_empty_pool_blocks_scale_up_without_panic():
    fleet = _FakeFleet(queue_size=10)
    proxmox = _FakeProxmox(["pve1", "pve2"], {})  # nothing tagged yet
    s = Scaler(_cfg(workers_per_node=2), fleet, proxmox)
    out = s.tick()
    assert out["action"] == "scale_up_blocked"
