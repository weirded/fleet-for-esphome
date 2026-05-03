"""Thin wrapper around proxmoxer for LXC start/stop/status."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from proxmoxer import ProxmoxAPI

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LxcState:
    vmid: int
    status: str  # "running" | "stopped" | other


class ProxmoxClient:
    """Manages a fixed pool of LXC containers identified by VMID."""

    def __init__(
        self,
        host: str,
        token_id: str,
        token_secret: str,
        node: str,
        verify_ssl: bool = True,
    ) -> None:
        # token_id format: "<user>@<realm>!<token-name>" — proxmoxer wants user + token_name split
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
        self._node = node

    def list_states(self, vmids: tuple[int, ...]) -> list[LxcState]:
        out: list[LxcState] = []
        existing = {int(c["vmid"]): c for c in self._api.nodes(self._node).lxc.get()}
        for vmid in vmids:
            entry = existing.get(vmid)
            if entry is None:
                logger.warning(
                    "vmid %d not found on node %s — skipping (check the LXC pool exists)",
                    vmid,
                    self._node,
                )
                continue
            out.append(LxcState(vmid=vmid, status=str(entry.get("status", "unknown"))))
        return out

    def running_vmids(self, vmids: tuple[int, ...]) -> list[int]:
        return [s.vmid for s in self.list_states(vmids) if s.status == "running"]

    def stopped_vmids(self, vmids: tuple[int, ...]) -> list[int]:
        return [s.vmid for s in self.list_states(vmids) if s.status == "stopped"]

    def start(self, vmid: int) -> None:
        logger.info("starting LXC %d on node %s", vmid, self._node)
        self._api.nodes(self._node).lxc(vmid).status.start.post()

    def stop(self, vmid: int) -> None:
        # `shutdown` is graceful (sends SIGTERM and waits); `stop` is hard kill.
        # We use shutdown so the worker has a chance to drain its current job
        # within the LXC's grace period.
        logger.info("shutting down LXC %d on node %s", vmid, self._node)
        self._api.nodes(self._node).lxc(vmid).status.shutdown.post()
