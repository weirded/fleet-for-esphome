"""Multi-node Proxmox VE client wrapper.

Identifies worker LXCs across all nodes by a configurable tag (default
``esphome-fleet-worker``). The scaler doesn't need a static VMID list
anymore — it asks the cluster what's there and reconciles.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from proxmoxer import ProxmoxAPI

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LxcState:
    node: str
    vmid: int
    status: str  # "running" | "stopped" | other


class ProxmoxClient:
    def __init__(
        self,
        host: str,
        token_id: str,
        token_secret: str,
        verify_ssl: bool = True,
    ) -> None:
        if "!" not in token_id:
            raise ValueError(
                "token_id must be of the form '<user>@<realm>!<tokenname>' "
                "(e.g. 'root@pam!scaler')"
            )
        user, _, token_name = token_id.partition("!")
        self._api = ProxmoxAPI(
            host,
            user=user,
            token_name=token_name,
            token_value=token_secret,
            verify_ssl=verify_ssl,
        )

    def list_online_nodes(self) -> list[str]:
        """All online Proxmox nodes in the cluster."""
        return [n["node"] for n in self._api.nodes.get() if n.get("status") == "online"]

    def list_workers_by_node(self, tag: str) -> dict[str, list[LxcState]]:
        """Discover all LXCs carrying ``tag`` across the cluster, grouped by node.

        Uses ``cluster/resources?type=vm`` for one round-trip. Returns the
        empty dict when no matching LXCs exist (e.g., fresh install).
        """
        out: dict[str, list[LxcState]] = defaultdict(list)
        for r in self._api.cluster.resources.get(type="vm"):
            if r.get("type") != "lxc":
                continue
            tags_raw = r.get("tags") or ""
            # Proxmox tags are semicolon-separated.
            if tag not in [t.strip() for t in tags_raw.split(";")]:
                continue
            out[r["node"]].append(
                LxcState(
                    node=r["node"],
                    vmid=int(r["vmid"]),
                    status=str(r.get("status", "unknown")),
                )
            )
        return dict(out)

    def start(self, node: str, vmid: int) -> None:
        logger.info("starting LXC %d on node %s", vmid, node)
        self._api.nodes(node).lxc(vmid).status.start.post()

    def stop(self, node: str, vmid: int) -> None:
        # Graceful shutdown — gives the worker process time to drain.
        logger.info("shutting down LXC %d on node %s", vmid, node)
        self._api.nodes(node).lxc(vmid).status.shutdown.post()
