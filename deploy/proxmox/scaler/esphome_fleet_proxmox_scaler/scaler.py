"""The reconciliation loop. Pure logic — Fleet/Proxmox clients are injected so it's testable."""

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


class _ProxmoxController(Protocol):
    def running_vmids(self, vmids: tuple[int, ...]) -> list[int]: ...  # noqa: E704
    def stopped_vmids(self, vmids: tuple[int, ...]) -> list[int]: ...  # noqa: E704
    def start(self, vmid: int) -> None: ...  # noqa: E704
    def stop(self, vmid: int) -> None: ...  # noqa: E704


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
        # Set to negative so cooldown does not block the first scale-down decision.
        self._last_scale_down_at = -float("inf")

    # --- public ---

    def desired_count(self, queue_size: int) -> int:
        """Compute how many workers we want running for this queue size.

        Always at least min_workers; never more than max_workers; otherwise
        ceil(queue_size / target_per_worker). Returning min_workers when
        queue is 0 is what keeps an "always-on" baseline available.
        """
        if queue_size <= 0:
            return self.config.min_workers
        raw = math.ceil(queue_size / self.config.target_per_worker)
        return max(self.config.min_workers, min(self.config.max_workers, raw))

    def tick(self) -> dict:
        """One reconcile pass. Returns a small status dict for callers/tests."""
        try:
            status = self.fleet.status()
        except Exception:
            logger.exception("fleet status fetch failed; skipping this tick")
            return {"action": "skip", "reason": "fleet_unreachable"}

        running = self.proxmox.running_vmids(self.config.vmids)
        stopped = self.proxmox.stopped_vmids(self.config.vmids)
        desired = self.desired_count(status.queue_size)
        current = len(running)

        logger.debug(
            "tick: queue_size=%d online_workers=%d running=%d desired=%d",
            status.queue_size, status.online_workers, current, desired,
        )

        if desired > current:
            return self._scale_up(stopped, desired - current)
        if desired < current:
            return self._scale_down(running, current - desired)
        return {"action": "noop", "running": current, "desired": desired}

    def loop(self) -> None:
        logger.info(
            "scaler started: pool=%s min=%d max=%d target_per_worker=%d poll=%ds cooldown=%ds",
            self.config.vmids,
            self.config.min_workers,
            self.config.max_workers,
            self.config.target_per_worker,
            self.config.poll_interval,
            self.config.cooldown_seconds,
        )
        while True:
            self.tick()
            time.sleep(self.config.poll_interval)

    # --- private ---

    def _scale_up(self, stopped: list[int], count: int) -> dict:
        # Lower-numbered VMIDs come up first — deterministic + matches the
        # operator's mental model of "the first slot in the pool is the one
        # that wakes first."
        candidates = sorted(stopped)[:count]
        if not candidates:
            logger.warning(
                "want to scale UP by %d but pool has no stopped vmids — operator should grow the pool",
                count,
            )
            return {"action": "scale_up_blocked", "wanted": count, "available": 0}
        for vmid in candidates:
            try:
                self.proxmox.start(vmid)
            except Exception:
                logger.exception("failed to start vmid=%d; will retry next tick", vmid)
        return {"action": "scale_up", "started": candidates}

    def _scale_down(self, running: list[int], count: int) -> dict:
        # Cooldown gate: don't churn workers up and down. Without this, a brief
        # burst-then-empty queue could thrash the LXCs.
        now = self._clock()
        if now - self._last_scale_down_at < self.config.cooldown_seconds:
            return {
                "action": "scale_down_cooldown",
                "remaining": self.config.cooldown_seconds - (now - self._last_scale_down_at),
            }
        # Stop the highest-numbered VMIDs first — symmetric with start order.
        candidates = sorted(running, reverse=True)[:count]
        for vmid in candidates:
            try:
                self.proxmox.stop(vmid)
            except Exception:
                logger.exception("failed to stop vmid=%d; will retry next tick", vmid)
        self._last_scale_down_at = now
        return {"action": "scale_down", "stopped": candidates}
