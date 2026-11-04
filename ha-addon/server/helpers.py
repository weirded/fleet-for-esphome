"""Shared helpers for the server — DRY utilities used across api.py, ui_api.py, main.py."""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
from pathlib import Path
from typing import Optional

from aiohttp import web


def ha_mode() -> str:
    """Return ``"addon"`` or ``"standalone"`` based on Supervisor presence.

    SI (WORKITEMS-1.6.2): explicit label for which deployment shape the
    server is running under, so every HA-coupled code path can log
    ``"skipped X (standalone mode)"`` instead of silently no-op'ing
    without an operator-visible signal.

    Detection order:
      1. ``HA_MODE`` env var — explicit user override, wins.
      2. ``SUPERVISOR_TOKEN`` env var — set by HA Supervisor on add-on
         install; absent in standalone Docker.

    Returns a plain string rather than a bool so log lines read
    naturally ("Running in %s mode", ha_mode()).
    """
    override = os.environ.get("HA_MODE", "").strip().lower()
    if override in ("addon", "standalone"):
        return override
    return "addon" if os.environ.get("SUPERVISOR_TOKEN") else "standalone"


def is_standalone() -> bool:
    """Convenience wrapper for ``ha_mode() == "standalone"``.

    Call sites that just want to gate a Supervisor-calling code path
    read better with this than with a string compare.
    """
    return ha_mode() == "standalone"


def json_error(message: str, status: int = 400) -> web.Response:
    """Return a JSON error response."""
    return web.json_response({"error": message}, status=status)


def safe_resolve(config_dir: str | Path, filename: str) -> Optional[Path]:
    """Resolve a filename within config_dir, preventing path traversal.

    Returns the resolved Path if safe, or None if the path escapes config_dir.
    """
    base = Path(config_dir).resolve()
    path = (base / filename).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return None
    return path


def constant_time_compare(a: str, b: str) -> bool:
    """Timing-safe string comparison to prevent timing attacks on tokens."""
    return secrets.compare_digest(a.encode(), b.encode())


def clamp(value: int, min_val: int, max_val: int) -> int:
    """Clamp an integer to a range."""
    return max(min_val, min(max_val, value))


# A DNS label: alphanumeric, inner hyphens allowed, 1-63 chars. A hostname
# is one or more of those joined by dots, with an optional trailing root dot.
_HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def is_valid_ota_address(address: str) -> bool:
    """True if *address* is a literal IP or a syntactically valid hostname.

    OTA addresses reach ``esphome upload --device <addr>`` and the
    server-side pre-flight ``ping`` as argv entries. Neither runs through a
    shell, so there is no command-injection surface -- but both parse
    leading-dash arguments as *flags*, so an address like ``-f`` or
    ``--help`` is interpreted rather than dialled. Everything the product
    legitimately produces here is either a literal address resolved by
    device_poller or a hostname the user typed, so require exactly that and
    reject the rest at the boundary.
    """
    if not address or len(address) > 253:
        return False
    try:
        ipaddress.ip_address(address)
        return True
    except ValueError:
        pass
    # Not a literal IP — accept a plain hostname/FQDN (e.g. "device.local").
    hostname = address[:-1] if address.endswith(".") else address
    if not hostname:
        return False
    return all(_HOSTNAME_LABEL_RE.match(label) for label in hostname.split("."))
