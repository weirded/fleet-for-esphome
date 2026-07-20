"""Environment-driven configuration for the scaler.

Multi-node model: the scaler discovers all online Proxmox nodes via the API
and reconciles a per-node target count of LXC workers. Default is 1 worker
per node ("each Proxmox node runs one ESPHome build worker"); per-node
overrides let you scale up beefier nodes or skip lightweight ones.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    fleet_url: str
    fleet_token: str

    proxmox_host: str
    proxmox_verify_ssl: bool
    proxmox_token_id: str
    proxmox_token_secret: str

    # Multi-node sizing.
    workers_per_node: int                                # default cap per node
    per_node_overrides: dict[str, int] = field(default_factory=dict)
    min_total_workers: int = 0                            # always-on baseline across all nodes
    max_total_workers: int | None = None                  # None = sum of effective per-node caps
    worker_tag: str = "esphome-fleet-worker"              # tag the scaler stamps on each worker LXC

    # Scheduling knobs (unchanged from v1).
    target_per_worker: int = 2
    poll_interval: int = 30
    cooldown_seconds: int = 600

    log_level: str = "INFO"

    def per_node_max(self, node: str) -> int:
        """Effective max worker count for the given node — override if set, else default."""
        return self.per_node_overrides.get(node, self.workers_per_node)

    def validate(self) -> None:
        if not self.fleet_url:
            raise ValueError("PROXMOX_SCALER_FLEET_URL is required")
        if not self.fleet_token:
            raise ValueError("PROXMOX_SCALER_FLEET_TOKEN is required")
        if not self.proxmox_host:
            raise ValueError("PROXMOX_SCALER_PROXMOX_HOST is required")
        if not self.proxmox_token_id or not self.proxmox_token_secret:
            raise ValueError(
                "PROXMOX_SCALER_PROXMOX_TOKEN_ID and "
                "PROXMOX_SCALER_PROXMOX_TOKEN_SECRET are required"
            )
        if self.workers_per_node < 0:
            raise ValueError("workers_per_node must be >= 0")
        for n, v in self.per_node_overrides.items():
            if v < 0:
                raise ValueError(f"per-node override {n}={v} must be >= 0")
        if self.min_total_workers < 0:
            raise ValueError("min_total_workers must be >= 0")
        if self.max_total_workers is not None and self.max_total_workers < self.min_total_workers:
            raise ValueError("max_total_workers must be >= min_total_workers")
        if self.target_per_worker < 1:
            raise ValueError("target_per_worker must be >= 1")
        if self.poll_interval < 1:
            raise ValueError("poll_interval must be >= 1")


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from e


def _get_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from e


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _parse_per_node_overrides(raw: str) -> dict[str, int]:
    """Parse PROXMOX_SCALER_PER_NODE_OVERRIDES.

    Format: ``node1:N,node2:N,...``. Whitespace around tokens is tolerated.
    """
    out: dict[str, int] = {}
    if not raw.strip():
        return out
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" not in piece:
            raise ValueError(
                f"PROXMOX_SCALER_PER_NODE_OVERRIDES entry {piece!r} must be 'node:count'"
            )
        node, _, count_s = piece.partition(":")
        node = node.strip()
        if not node:
            raise ValueError(
                f"PROXMOX_SCALER_PER_NODE_OVERRIDES entry {piece!r}: empty node name"
            )
        try:
            out[node] = int(count_s.strip())
        except ValueError as e:
            raise ValueError(
                f"PROXMOX_SCALER_PER_NODE_OVERRIDES entry {piece!r}: count must be int"
            ) from e
    return out


def from_env() -> Config:
    return Config(
        fleet_url=os.environ.get("PROXMOX_SCALER_FLEET_URL", "").rstrip("/"),
        fleet_token=os.environ.get("PROXMOX_SCALER_FLEET_TOKEN", ""),
        proxmox_host=os.environ.get("PROXMOX_SCALER_PROXMOX_HOST", ""),
        proxmox_verify_ssl=_get_bool("PROXMOX_SCALER_PROXMOX_VERIFY_SSL", True),
        proxmox_token_id=os.environ.get("PROXMOX_SCALER_PROXMOX_TOKEN_ID", ""),
        proxmox_token_secret=os.environ.get("PROXMOX_SCALER_PROXMOX_TOKEN_SECRET", ""),
        workers_per_node=_get_int("PROXMOX_SCALER_WORKERS_PER_NODE", 1),
        per_node_overrides=_parse_per_node_overrides(
            os.environ.get("PROXMOX_SCALER_PER_NODE_OVERRIDES", "")
        ),
        min_total_workers=_get_int("PROXMOX_SCALER_MIN_TOTAL_WORKERS", 0),
        max_total_workers=_get_optional_int("PROXMOX_SCALER_MAX_TOTAL_WORKERS"),
        worker_tag=os.environ.get("PROXMOX_SCALER_WORKER_TAG", "esphome-fleet-worker"),
        target_per_worker=_get_int("PROXMOX_SCALER_TARGET_PER_WORKER", 2),
        poll_interval=_get_int("PROXMOX_SCALER_POLL_INTERVAL", 30),
        cooldown_seconds=_get_int("PROXMOX_SCALER_COOLDOWN_SECONDS", 600),
        log_level=os.environ.get("PROXMOX_SCALER_LOG_LEVEL", "INFO").upper(),
    )
