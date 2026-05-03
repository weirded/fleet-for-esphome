"""Thin client for distributed-esphome's public worker API."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FleetStatus:
    esphome_version: str
    online_workers: int
    queue_size: int


class FleetClient:
    """Hits `GET /api/v1/status` with a bearer token. That's all this scaler needs."""

    def __init__(self, base_url: str, token: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"

    def status(self) -> FleetStatus:
        url = f"{self.base_url}/api/v1/status"
        resp = self._session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return FleetStatus(
            esphome_version=body.get("esphome_version", ""),
            online_workers=int(body.get("online_workers", 0)),
            queue_size=int(body.get("queue_size", 0)),
        )

    def close(self) -> None:
        self._session.close()
