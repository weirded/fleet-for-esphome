"""Environment-driven configuration for the scaler."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    fleet_url: str
    fleet_token: str

    proxmox_host: str
    proxmox_verify_ssl: bool
    proxmox_token_id: str
    proxmox_token_secret: str
    proxmox_node: str

    vmids: tuple[int, ...]

    min_workers: int
    max_workers: int
    target_per_worker: int
    poll_interval: int
    cooldown_seconds: int

    log_level: str

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
        if not self.proxmox_node:
            raise ValueError("PROXMOX_SCALER_PROXMOX_NODE is required")
        if not self.vmids:
            raise ValueError("PROXMOX_SCALER_VMIDS must contain at least one VMID")
        if self.min_workers < 0:
            raise ValueError("min_workers must be >= 0")
        if self.max_workers < self.min_workers:
            raise ValueError("max_workers must be >= min_workers")
        if self.max_workers > len(self.vmids):
            raise ValueError(
                f"max_workers ({self.max_workers}) exceeds pool size ({len(self.vmids)}) — "
                "either grow the LXC pool or lower max_workers"
            )
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


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _get_vmids(name: str) -> tuple[int, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError as e:
            raise ValueError(
                f"{name} must be a comma-separated list of integers; got {piece!r}"
            ) from e
    return tuple(out)


def from_env() -> Config:
    return Config(
        fleet_url=os.environ.get("PROXMOX_SCALER_FLEET_URL", "").rstrip("/"),
        fleet_token=os.environ.get("PROXMOX_SCALER_FLEET_TOKEN", ""),
        proxmox_host=os.environ.get("PROXMOX_SCALER_PROXMOX_HOST", ""),
        proxmox_verify_ssl=_get_bool("PROXMOX_SCALER_PROXMOX_VERIFY_SSL", True),
        proxmox_token_id=os.environ.get("PROXMOX_SCALER_PROXMOX_TOKEN_ID", ""),
        proxmox_token_secret=os.environ.get("PROXMOX_SCALER_PROXMOX_TOKEN_SECRET", ""),
        proxmox_node=os.environ.get("PROXMOX_SCALER_PROXMOX_NODE", ""),
        vmids=_get_vmids("PROXMOX_SCALER_VMIDS"),
        min_workers=_get_int("PROXMOX_SCALER_MIN_WORKERS", 0),
        max_workers=_get_int("PROXMOX_SCALER_MAX_WORKERS", 3),
        target_per_worker=_get_int("PROXMOX_SCALER_TARGET_PER_WORKER", 2),
        poll_interval=_get_int("PROXMOX_SCALER_POLL_INTERVAL", 30),
        cooldown_seconds=_get_int("PROXMOX_SCALER_COOLDOWN_SECONDS", 600),
        log_level=os.environ.get("PROXMOX_SCALER_LOG_LEVEL", "INFO").upper(),
    )
