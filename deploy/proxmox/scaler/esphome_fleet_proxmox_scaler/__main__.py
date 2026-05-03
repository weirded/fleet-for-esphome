"""Entry point: wire env config → real clients → scaler loop."""

from __future__ import annotations

import logging
import sys

from . import config as config_mod
from .fleet import FleetClient
from .proxmox import ProxmoxClient
from .scaler import Scaler


def main() -> int:
    cfg = config_mod.from_env()
    try:
        cfg.validate()
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    fleet = FleetClient(cfg.fleet_url, cfg.fleet_token)
    proxmox = ProxmoxClient(
        host=cfg.proxmox_host,
        token_id=cfg.proxmox_token_id,
        token_secret=cfg.proxmox_token_secret,
        node=cfg.proxmox_node,
        verify_ssl=cfg.proxmox_verify_ssl,
    )
    scaler = Scaler(cfg, fleet, proxmox)

    try:
        scaler.loop()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("shutdown requested; exiting cleanly")
        return 0
    finally:
        fleet.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
