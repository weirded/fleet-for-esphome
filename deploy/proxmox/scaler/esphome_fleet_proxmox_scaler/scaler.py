"""Reconciliation loop for the multi-node LXC pool."""

from __future__ import annotations

import logging
import math
import time
from typing import Protocol

from .config import Config

logger = logging.getLogger(__name__)


class _FleetReader(Protocol):
    def status(self) -> "_FleetStatusLike": ...  # noqa: D401, E704


class _FleetStatusLike(Protocol):
    queue_size: int
    online_workers: int


class _LxcState(Protocol):
    node: str
    vmid: int
    status: str


class _ProxmoxController(Protocol):
    def list_online_nodes(self) -> list[str]: ...  # noqa: E704
    def list_workers_by_node(self, tag: str) -> dict[str, list[_LxcState]]: ...  # noqa: E704
    def start(self, node: str, vmid: int) -> None: ...  # noqa: E704
    def stop(self, node: str, vmid: int) -> None: ...  # noqa: E704


class Scaler:
    def __init__(
        self,
        config: Config,
        fleet: _FleetReader,
        proxmox: _ProxmoxController,
        clock: callable = time.monotonic,
    ) -> None:
        self.config = config
        self.fleet = fleet
        self.proxmox = proxmox
        self._clock = clock
        self._last_scale_down_at = -float("inf")

    # --- desired count ---

    def desired_count(self, queue_size: int, max_total: int) -> int:
        if queue_size <= 0:
            return self.config.min_total_workers
        raw = math.ceil(queue_size / self.config.target_per_worker)
        return max(self.config.min_total_workers, min(max_total, raw))

    # --- single tick ---

    def tick(self) -> dict:
        try:
            status = self.fleet.status()
        except Exception:
            logger.exception("fleet status fetch failed; skipping this tick")
            return {"action": "skip", "reason": "fleet_unreachable"}

        try:
            nodes = self.proxmox.list_online_nodes()
            pool = self.proxmox.list_workers_by_node(self.config.worker_tag)
        except Exception:
            logger.exception("proxmox state fetch failed; skipping this tick")
            return {"action": "skip", "reason": "proxmox_unreachable"}

        per_node_max = {n: self.config.per_node_max(n) for n in nodes}
        sum_caps = sum(per_node_max.values())
        if self.config.max_total_workers is not None:
            max_total = min(self.config.max_total_workers, sum_caps)
        else:
            max_total = sum_caps

        running_per_node: dict[str, list] = {}
        stopped_per_node: dict[str, list] = {}
        for node in nodes:
            lxcs = pool.get(node, [])
            running_per_node[node] = [l for l in lxcs if l.status == "running"]
            stopped_per_node[node] = [l for l in lxcs if l.status == "stopped"]

        current_total = sum(len(rs) for rs in running_per_node.values())
        desired_total = self.desired_count(status.queue_size, max_total)

        logger.debug(
            "tick: queue=%d online_workers=%d running=%d desired=%d max_total=%d",
            status.queue_size, status.online_workers, current_total, desired_total, max_total,
        )

        if desired_total > current_total:
            return self._scale_up(
                stopped_per_node, running_per_node, per_node_max, desired_total - current_total
            )
        if desired_total < current_total:
            return self._scale_down(running_per_node, current_total - desired_total)
        return {
            "action": "noop",
            "running": current_total,
            "desired": desired_total,
            "per_node_running": {n: len(r) for n, r in running_per_node.items()},
        }

    def loop(self) -> None:
        logger.info(
            "scaler started: workers_per_node=%d overrides=%s min_total=%d max_total=%s "
            "tag=%s target_per_worker=%d poll=%ds cooldown=%ds",
            self.config.workers_per_node,
            self.config.per_node_overrides,
            self.config.min_total_workers,
            self.config.max_total_workers if self.config.max_total_workers is not None else "auto",
            self.config.worker_tag,
            self.config.target_per_worker,
            self.config.poll_interval,
            self.config.cooldown_seconds,
        )
        while True:
            self.tick()
            time.sleep(self.config.poll_interval)

    # --- scale up ---

    def _scale_up(
        self,
        stopped_per_node: dict[str, list],
        running_per_node: dict[str, list],
        per_node_max: dict[str, int],
        count: int,
    ) -> dict:
        """Pick stopped LXCs to start. Spread evenly: prefer nodes with the most
        free capacity (cap - current_running). Within a node, prefer lowest VMID first.
        """
        started: list[tuple[str, int]] = []
        for _ in range(count):
            # Recompute per-node free capacity each iteration so successive picks see the update.
            free_capacity = {
                n: per_node_max[n] - len(running_per_node.get(n, []))
                for n in per_node_max
            }
            # Filter to nodes with free capacity AND a stopped LXC available.
            eligible = [
                n for n, cap in free_capacity.items()
                if cap > 0 and stopped_per_node.get(n)
            ]
            if not eligible:
                break
            # Pick the node with the most free capacity (tie: alphabetical).
            best = max(eligible, key=lambda n: (free_capacity[n], -ord(n[0]) if n else 0))
            # Lowest VMID first on that node.
            lxc = sorted(stopped_per_node[best], key=lambda l: l.vmid)[0]
            try:
                self.proxmox.start(lxc.node, lxc.vmid)
            except Exception:
                logger.exception(
                    "failed to start node=%s vmid=%d; will retry next tick", lxc.node, lxc.vmid
                )
                continue
            started.append((lxc.node, lxc.vmid))
            # Update local view so the next iteration sees this worker as running.
            stopped_per_node[best] = [l for l in stopped_per_node[best] if l.vmid != lxc.vmid]
            running_per_node.setdefault(best, []).append(lxc)
        if not started:
            return {"action": "scale_up_blocked", "wanted": count}
        return {"action": "scale_up", "started": started}

    # --- scale down ---

    def _scale_down(self, running_per_node: dict[str, list], count: int) -> dict:
        now = self._clock()
        if now - self._last_scale_down_at < self.config.cooldown_seconds:
            return {
                "action": "scale_down_cooldown",
                "remaining": self.config.cooldown_seconds - (now - self._last_scale_down_at),
            }
        stopped: list[tuple[str, int]] = []
        for _ in range(count):
            # Pick the node with the most running workers (load-shedding).
            eligible = [(n, lxcs) for n, lxcs in running_per_node.items() if lxcs]
            if not eligible:
                break
            n, lxcs = max(eligible, key=lambda nl: len(nl[1]))
            # Stop highest VMID first on that node.
            lxc = sorted(lxcs, key=lambda l: l.vmid, reverse=True)[0]
            try:
                self.proxmox.stop(lxc.node, lxc.vmid)
            except Exception:
                logger.exception(
                    "failed to stop node=%s vmid=%d; will retry next tick", lxc.node, lxc.vmid
                )
                continue
            stopped.append((lxc.node, lxc.vmid))
            running_per_node[n] = [l for l in lxcs if l.vmid != lxc.vmid]
        if stopped:
            self._last_scale_down_at = now
            return {"action": "scale_down", "stopped": stopped}
        return {"action": "scale_down_blocked"}
