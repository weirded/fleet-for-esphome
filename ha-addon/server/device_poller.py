"""mDNS device discovery and aioesphomeapi version polling."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

try:
    from zeroconf import ServiceBrowser, Zeroconf
    from zeroconf.asyncio import AsyncZeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    logger.warning("zeroconf not available; mDNS discovery disabled")
    ZEROCONF_AVAILABLE = False

try:
    import aioesphomeapi
    AIOESPHOMEAPI_AVAILABLE = True
except ImportError:
    logger.warning("aioesphomeapi not available; device version polling disabled")
    AIOESPHOMEAPI_AVAILABLE = False


# Bug #3 (1.6.1): aioesphomeapi logs "disconnect request failed" at
# ERROR whenever a device tears down its connection mid-request. This
# fires on every OTA reboot — expected behaviour, not an incident —
# and pollutes the add-on log with a multi-line APIConnectionError
# traceback each time a device transitions through its new firmware.
# Install a targeted filter that downgrades exactly that record to
# DEBUG; genuine errors from the same logger stay at ERROR so real
# connection problems still surface.
class _AioesphomeapiDisconnectFilter(logging.Filter):
    """Drop the expected-on-OTA 'disconnect request failed' record.

    Bug #3 (1.6.1) — PR review follow-up: the first implementation
    mutated ``record.levelno``/``levelname`` and returned ``True`` so
    the record would still flow through. That was wrong: by the time
    ``filter()`` runs, ``Logger.callHandlers`` has already selected
    handlers based on the *original* ERROR level, so mutating the
    level just changes the rendered tag — the ERROR handler still
    emits the message, now with a misleading "DEBUG" label. The
    correct behaviour is to drop it outright (``return False``). We
    still emit the "this happened" signal via a DEBUG log on the
    ``device_poller`` logger (a different logger, so no recursion)
    for operators who need it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and "disconnect request failed" in record.getMessage().lower():
            logger.debug(
                "aioesphomeapi.connection disconnect-request failure during "
                "OTA-reboot window (expected): %s",
                record.getMessage(),
            )
            return False
        return True


logging.getLogger("aioesphomeapi.connection").addFilter(_AioesphomeapiDisconnectFilter())

try:
    import icmplib  # noqa: F401
    _PING_AVAILABLE = True
except ImportError:
    logger.warning("icmplib not available; ping-based liveness fallback disabled")
    _PING_AVAILABLE = False

if TYPE_CHECKING:
    from zeroconf import ServiceBrowser, Zeroconf  # noqa: F811
    from zeroconf.asyncio import AsyncZeroconf  # noqa: F811

ESPHOME_SERVICE = "_esphomelib._tcp.local."
DEVICE_CACHE_FILE = Path("/data/device_cache.json")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_ip_literal(addr: str) -> bool:
    """Bug #18 (1.6.1): True when *addr* parses as an IPv4 or IPv6
    literal. Used by :meth:`DevicePoller.resolve_ota_address` to
    decide whether to trust an address override; a literal is
    always better than a ``.local`` hostname on networks where the
    worker's Docker container can't resolve mDNS.
    """
    if not addr:
        return False
    import ipaddress  # noqa: PLC0415
    try:
        ipaddress.ip_address(addr)
    except (ValueError, TypeError):
        return False
    return True


@dataclass
class Device:
    name: str
    ip_address: str
    online: bool = False
    running_version: Optional[str] = None
    compilation_time: Optional[str] = None  # e.g. "2026-04-23 06:13:56 -0700"
    last_seen: Optional[datetime] = None
    compile_target: Optional[str] = None  # e.g. "living_room.yaml"
    mac_address: Optional[str] = None  # e.g. "AA:BB:CC:DD:EE:FF"
    # How was the IP resolved? One of: "mdns", "wifi_use_address",
    # "ethernet_use_address", "openthread_use_address", "wifi_static_ip",
    # "ethernet_static_ip", "mdns_default" (the {name}.local fallback).
    # Surfaced in the UI under the IP so users can see how each device's
    # address was determined (#184).
    address_source: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ip_address": self.ip_address,
            "online": self.online,
            "running_version": self.running_version,
            "compilation_time": self.compilation_time,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "compile_target": self.compile_target,
            "mac_address": self.mac_address,
            "address_source": self.address_source,
        }


class DevicePoller:
    """
    Discovers ESPHome devices via mDNS and polls their firmware version
    via the native API.
    """

    def __init__(self, poll_interval: int = 60) -> None:
        self._poll_interval = poll_interval
        self._devices: dict[str, Device] = {}  # keyed by device name
        self._compile_targets: list[str] = []
        self._name_to_target: dict[str, str] = {}
        self._encryption_keys: dict[str, str] = {}  # device_name → noise_psk (base64)
        self._address_overrides: dict[str, str] = {}  # device_name → use_address
        self._address_sources: dict[str, str] = {}  # device_name → e.g. "wifi_use_address"
        self._lock = asyncio.Lock()
        self._zeroconf: Optional[AsyncZeroconf] = None
        self._browser: Optional[ServiceBrowser] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, app: object = None) -> None:
        """Start mDNS listener and background polling task."""
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._load_cache()
        if ZEROCONF_AVAILABLE:
            await self._start_mdns()
        else:
            logger.warning("Skipping mDNS discovery (zeroconf unavailable)")

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("DevicePoller started (poll_interval=%ds)", self._poll_interval)

    async def stop(self) -> None:
        """Stop background tasks and release resources."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._zeroconf is not None:
            try:
                await self._zeroconf.async_close()
            except Exception:
                logger.exception("Error closing zeroconf")
        logger.info("DevicePoller stopped")

    # ------------------------------------------------------------------
    # mDNS
    # ------------------------------------------------------------------

    async def _start_mdns(self) -> None:
        try:
            self._zeroconf = AsyncZeroconf()
            self._browser = ServiceBrowser(
                self._zeroconf.zeroconf,
                ESPHOME_SERVICE,
                handlers=[self._on_service_state_change],
            )
            logger.info("mDNS ServiceBrowser started for %s", ESPHOME_SERVICE)
        except Exception:
            logger.exception("Failed to start mDNS browser")

    def _on_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: object,
    ) -> None:
        """Callback invoked by zeroconf on service add/remove/update."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            self._loop.create_task,
            self._handle_service_change(zeroconf, service_type, name, state_change),
        )

    async def _handle_service_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: object,
    ) -> None:
        try:
            from zeroconf import ServiceStateChange  # noqa: PLC0415
            # C.8: get_running_loop is the modern equivalent of get_event_loop
            # when we're already inside a coroutine — and is the only one that
            # works in 3.12+.
            info = await asyncio.get_running_loop().run_in_executor(
                None, zeroconf.get_service_info, service_type, name
            )
            if info is None:
                return

            device_name = info.name.replace(f".{service_type}", "").strip()
            # ESPHome device names may have the service suffix embedded
            if "." in device_name:
                device_name = device_name.split(".")[0]

            ip = self._extract_address(info)

            # Extract version from TXT record
            txt_version: Optional[str] = None
            if info.properties:
                for key, val in info.properties.items():
                    k = key.decode() if isinstance(key, bytes) else key
                    if k == "version":
                        txt_version = val.decode() if isinstance(val, bytes) else val
                        break

            async with self._lock:
                if state_change == ServiceStateChange.Removed:
                    existing_key = self._find_existing_device_key(device_name)
                    if existing_key:
                        self._devices[existing_key].online = False
                    return

                # Look up an existing device by normalized name (handles
                # hyphen/underscore differences between YAML's esphome.name
                # and the mDNS-advertised name) so mDNS discovery merges
                # into the YAML-derived row instead of creating a duplicate
                # (bug #179).
                existing_key = self._find_existing_device_key(device_name)
                if existing_key is None:
                    compile_target = self._map_target(device_name)
                    self._devices[device_name] = Device(
                        name=device_name,
                        ip_address=ip or "",
                        compile_target=compile_target,
                        address_source="mdns" if ip else None,
                    )
                    existing_key = device_name

                dev = self._devices[existing_key]
                dev.online = True
                dev.last_seen = _utcnow()
                if ip:
                    dev.ip_address = ip
                    # mDNS only "wins" over the YAML-derived source if the
                    # YAML had no explicit address (was just {name}.local).
                    # Explicit user choices like wifi.use_address /
                    # wifi.manual_ip.static_ip stay authoritative — that
                    # mismatch is itself useful information.
                    if dev.address_source in (None, "mdns_default"):
                        dev.address_source = "mdns"
                if txt_version:
                    dev.running_version = txt_version
                self._save_cache()

            # #238: only open an aioesphomeapi connection when we genuinely
            # need to backfill information mDNS doesn't carry — the device's
            # ``mac_address`` and ``compilation_time``. Once those are set,
            # subsequent mDNS announces (every ~75 % of the TXT TTL, i.e.
            # roughly once a minute under default ESPHome settings) update
            # ``last_seen`` + ``running_version`` from the TXT record and do
            # NOT spawn a fresh connection. The legacy "always poll" path
            # is gated behind the ``device_native_api_poll`` setting (default
            # False) for power users who explicitly want every-tick polling.
            dev_now = self._devices.get(existing_key)
            needs_backfill = (
                dev_now is not None
                and dev_now.compilation_time is None
                and dev_now.mac_address is None
            )
            if needs_backfill or self._legacy_native_poll():
                query_addr = (
                    self._address_override_for(existing_key)
                    or self._address_override_for(device_name)
                    or ip
                )
                if query_addr:
                    # C.8: create_task is the modern equivalent of ensure_future
                    # when we're scheduling a coroutine on the running loop.
                    asyncio.create_task(self._query_device(existing_key, query_addr))

        except Exception:
            logger.exception("Error handling mDNS service change for %s", name)

    @staticmethod
    def _extract_address(info: object) -> Optional[str]:
        """Extract a single human-readable IP address from a zeroconf ServiceInfo.

        Handles both IPv4 (4-byte) and IPv6 (16-byte) packed addresses, which
        is required for Thread devices that advertise via SRP/mDNS over IPv6
        AAAA records (bug #179). Prefers IPv4 when both are present.
        """
        # python-zeroconf provides parsed_addresses() in modern versions —
        # try it first since it handles both families.
        try:
            parsed = info.parsed_addresses()  # type: ignore[attr-defined]
        except Exception:
            parsed = None

        if parsed:
            v4 = [a for a in parsed if "." in a]
            if v4:
                return v4[0]
            return parsed[0]

        # Fall back to manual parsing of the packed bytes
        addrs = getattr(info, "addresses", None) or []
        if not addrs:
            return None
        import socket  # noqa: PLC0415
        v4 = [a for a in addrs if len(a) == 4]
        if v4:
            try:
                return socket.inet_ntoa(v4[0])
            except OSError:
                pass
        v6 = [a for a in addrs if len(a) == 16]
        if v6:
            try:
                return socket.inet_ntop(socket.AF_INET6, v6[0])
            except (OSError, ValueError):
                pass
        return None

    def _find_existing_device_key(self, device_name: str) -> Optional[str]:
        """Return the key under which *device_name* is already stored, or None.

        Matches by hyphen/underscore-normalized name so an mDNS-discovered
        ``my_device`` (mDNS replaces hyphens) merges with a YAML-derived
        ``my-device`` row instead of creating a duplicate (bug #179).
        """
        if device_name in self._devices:
            return device_name
        norm = self._normalize(device_name)
        for key in self._devices:
            if self._normalize(key) == norm:
                return key
        return None

    # ------------------------------------------------------------------
    # Ping liveness check
    # ------------------------------------------------------------------

    async def _ping_device(self, name: str, ip: str) -> bool:
        """Ping a device to check if it is reachable. Returns True if alive.

        Tries unprivileged datagram ICMP first (no caps needed when the host's
        ``net.ipv4.ping_group_range`` allows it), then falls back to a raw
        socket — the addon container is granted ``NET_RAW`` via
        ``ha-addon/config.yaml`` for the HAOS case where the kernel default
        ``1 0`` disables unprivileged ICMP (#206). Only called when the API
        connection fails and icmplib is installed; guarded by
        ``_PING_AVAILABLE`` at the call site.
        """
        try:
            from icmplib import SocketPermissionError, async_ping  # noqa: PLC0415
            try:
                host = await async_ping(ip, count=1, timeout=2, privileged=False)
            except SocketPermissionError:
                host = await async_ping(ip, count=1, timeout=2, privileged=True)
            return host.is_alive
        except Exception:
            logger.debug("Ping failed for device %s at %s", name, ip, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # API polling
    # ------------------------------------------------------------------

    @staticmethod
    def _legacy_native_poll() -> bool:
        """#238: read the ``device_native_api_poll`` opt-in.

        Defaults to ``False`` — the device poller runs in mDNS-first mode
        and skips the every-tick blanket API fan-out. ``True`` restores
        the pre-1.7.1 behaviour where every device gets a fresh
        ``aioesphomeapi`` connection on every ``device_poll_interval``
        tick (and on every mDNS state change). Read fresh on every
        decision so a Settings drawer flip takes effect without
        restarting the poller.
        """
        try:
            from settings import get_settings  # noqa: PLC0415
            return bool(get_settings().device_native_api_poll)
        except Exception:
            return False

    async def _poll_loop(self) -> None:
        """Periodically reconcile known-device state.

        #238: in steady state (``device_native_api_poll = False``,
        the default), this loop does NOT open native-API connections to
        devices on every tick. mDNS provides liveness + ``running_version``
        via TXT records, and ``compilation_time`` / ``mac_address`` are
        backfilled once on first sight (see ``_handle_service_change``).
        The loop's remaining responsibilities are:

          1. **Fallback poll for non-mDNS devices** — devices whose
             ``last_seen`` is stale (older than 2 × poll interval) get a
             single API connect attempt to confirm liveness. Covers
             Ethernet boards, OpenThread devices, ``mdns: enabled: false``
             configs, and devices on a network where the mDNS proxy is
             flaky. Devices recently announced via mDNS are skipped.
          2. **Stale → offline transition** — a device whose mDNS TTL
             has elapsed AND whose API fallback failed flips to
             ``online = False``.
          3. **TTL purge** of stray mDNS-only neighbours.
          4. **Settings refresh** so a Settings drawer change to
             ``device_poll_interval`` takes effect on the next tick.

        ``device_native_api_poll = True`` restores the pre-1.7.1
        every-tick fan-out for users who want it.
        """
        while self._running:
            async with self._lock:
                snapshot = dict(self._devices)

            legacy = self._legacy_native_poll()
            now = _utcnow()
            mdns_trust_window = timedelta(seconds=2 * self._poll_interval)

            tasks = []
            for name, dev in snapshot.items():
                addr = self._address_override_for(name) or dev.ip_address
                if not addr:
                    continue
                if legacy:
                    # Pre-1.7.1 every-tick fan-out — opt-in.
                    tasks.append(self._query_device(name, addr))
                    continue
                # mDNS-first: only fall back to an API connect when we
                # haven't heard from this device on mDNS in a while.
                seen_recently = (
                    dev.last_seen is not None
                    and now - dev.last_seen <= mdns_trust_window
                )
                if seen_recently:
                    continue
                tasks.append(self._query_device(name, addr))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # #60: TTL — remove devices that haven't been seen in 4 hours
            # AND have no compile_target (no YAML file). Real configured
            # devices persist even when offline; only stray mDNS discoveries
            # from retired/unplugged/neighbor devices get purged.
            ttl_cutoff = _utcnow() - timedelta(hours=4)
            async with self._lock:
                expired = [
                    key for key, dev in self._devices.items()
                    if dev.compile_target is None
                    and not dev.online
                    and dev.last_seen is not None
                    and dev.last_seen < ttl_cutoff
                ]
                for key in expired:
                    del self._devices[key]
                if expired:
                    logger.info("TTL expired %d stale device(s): %s", len(expired), expired)
                    self._save_cache()

            # SP.8: read the live poll interval from Settings so drawer
            # edits take effect on the next iteration.
            try:
                from settings import get_settings  # noqa: PLC0415
                self._poll_interval = get_settings().device_poll_interval
            except Exception:
                logger.debug("Could not refresh poll_interval from settings; keeping cached value", exc_info=True)
            await asyncio.sleep(self._poll_interval)

    async def _query_device(self, name: str, ip: str) -> None:
        """Connect to device, fetch device_info, disconnect."""
        if not AIOESPHOMEAPI_AVAILABLE:
            return
        try:
            noise_psk = self._encryption_keys.get(name)
            client = aioesphomeapi.APIClient(ip, 6053, password=None, noise_psk=noise_psk)
            await client.connect(login=True)
            try:
                info = await client.device_info()
                async with self._lock:
                    dev = self._devices.get(name)
                    if dev:
                        dev.running_version = info.esphome_version
                        dev.compilation_time = getattr(info, "compilation_time", None) or None
                        dev.mac_address = getattr(info, "mac_address", None) or None
                        dev.online = True
                        dev.last_seen = _utcnow()
                        self._save_cache()
            finally:
                await client.disconnect()
        except Exception as exc:
            exc_str = str(exc).lower()
            if "encryption" in exc_str:
                # Device is reachable but requires encryption and we don't have
                # the key (or the key is wrong). No need to ping — mark online.
                async with self._lock:
                    dev = self._devices.get(name)
                    if dev:
                        dev.online = True
                        dev.last_seen = _utcnow()
                        self._save_cache()
                logger.debug("Device %s at %s requires encryption — marked online", name, ip)
                return

            # API failed for a non-encryption reason — fall back to ping so we
            # can still report liveness even when the native API is unavailable.
            ping_alive = await self._ping_device(name, ip) if _PING_AVAILABLE else False
            async with self._lock:
                dev = self._devices.get(name)
                if dev:
                    if ping_alive:
                        dev.online = True
                        dev.last_seen = _utcnow()
                        self._save_cache()
                        logger.debug(
                            "Device %s at %s: API failed (%s), ping succeeded — marked online",
                            name, ip, exc,
                        )
                    else:
                        dev.online = False
                        logger.debug("Could not reach device %s at %s: %s", name, ip, exc)

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _load_cache(self) -> None:
        """Populate _devices from last-known state so UI has data before mDNS fires.

        Only the STABLE bits are cached: running_version, compilation_time,
        mac_address. The IP address and address_source are deliberately NOT
        cached because they can change between restarts (DHCP lease renewal,
        WiFi reconfiguration, etc.). Stale cached IPs would point at the
        wrong device. Both are repopulated by update_compile_targets at
        startup (from the YAML's get_device_address) and then overridden
        by mDNS discovery as devices come back online (#187).
        """
        try:
            if not DEVICE_CACHE_FILE.exists():
                return
            data = json.loads(DEVICE_CACHE_FILE.read_text())
            for name, info in data.items():
                compile_target = self._map_target(name)
                self._devices[name] = Device(
                    name=name,
                    ip_address="",  # NOT from cache — see docstring
                    online=False,  # unknown until mDNS confirms
                    running_version=info.get("running_version"),
                    compilation_time=info.get("compilation_time"),
                    compile_target=compile_target,
                    mac_address=info.get("mac_address"),
                    # address_source intentionally not cached
                )
            logger.info("Loaded %d devices from cache", len(data))
        except Exception:
            logger.debug("Failed to load device cache", exc_info=True)

    def _save_cache(self) -> None:
        """Persist current device versions and MAC addresses to disk.

        Does NOT persist ip_address or address_source — see _load_cache
        docstring for why.

        #41: also broadcasts a ``devices_changed`` event so HA integrations
        refresh in real time instead of waiting on the coordinator poll.
        Piggy-backs on the existing save-point, which fires on every
        material device transition (online/offline, running_version,
        mac_address, compilation_time).
        """
        try:
            data = {
                name: {
                    "running_version": dev.running_version,
                    "compilation_time": dev.compilation_time,
                    "mac_address": dev.mac_address,
                }
                for name, dev in self._devices.items()
            }
            DEVICE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = DEVICE_CACHE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(DEVICE_CACHE_FILE)
        except Exception:
            logger.debug("Failed to save device cache", exc_info=True)
        try:
            from event_bus import EVENT_DEVICES_CHANGED, broadcast  # noqa: PLC0415
            broadcast(EVENT_DEVICES_CHANGED)
        except Exception:
            logger.debug("event_bus broadcast failed", exc_info=True)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def update_compile_targets(
        self,
        targets: list[str],
        name_to_target: Optional[dict[str, str]] = None,
        encryption_keys: Optional[dict[str, str]] = None,
        address_overrides: Optional[dict[str, str]] = None,
        address_sources: Optional[dict[str, str]] = None,
    ) -> None:
        """
        Inform the poller about known YAML targets so it can map device
        names to compile targets.  Also re-maps existing devices.

        *name_to_target* maps ESPHome device names (and filename stems) to
        YAML filenames, handling cases where ``esphome.name`` differs from
        the filename.

        *encryption_keys* maps device names to base64-encoded noise PSK keys
        for devices that require API encryption.

        *address_overrides* maps device names to the canonical address from
        ``scanner.get_device_address`` (always populated, may be ``{name}.local``).

        *address_sources* maps device names to where the address came from
        (e.g. ``wifi_use_address``, ``ethernet_static_ip``, ``mdns_default``).
        Surfaced under the IP in the UI (#184).
        """
        self._compile_targets = list(targets)
        self._name_to_target = name_to_target or {}
        self._encryption_keys = encryption_keys or {}
        self._address_overrides = address_overrides or {}
        self._address_sources = address_sources or {}
        for dev in self._devices.values():
            dev.compile_target = self._map_target(dev.name)

        # #59: remove stale proactive entries — devices that were pre-created
        # for a YAML target that no longer exists and have never been seen
        # on the network. A proactive entry has last_seen=None (never
        # discovered via mDNS/API), vs a real device which got a timestamp
        # when the poller first connected.
        stale_keys = [
            key for key, dev in self._devices.items()
            if dev.compile_target is None
            and dev.last_seen is None
            and not dev.online
        ]
        for key in stale_keys:
            del self._devices[key]

        # Proactively create Device entries for every YAML target. Now that
        # build_name_to_target_map populates address_overrides for ALL targets
        # (via get_device_address, which falls back to {name}.local), every
        # YAML row exists before mDNS discovery — so the mDNS handler merges
        # into it instead of creating a duplicate (bug #179).
        for device_name, addr in self._address_overrides.items():
            source = self._address_sources.get(device_name)
            existing_key = self._find_existing_device_key(device_name)
            if existing_key is None:
                compile_target = self._map_target(device_name)
                self._devices[device_name] = Device(
                    name=device_name,
                    ip_address=addr,
                    online=False,
                    compile_target=compile_target,
                    address_source=source,
                )
                # DL.3: promoted from DEBUG so operators can see the
                # poller picking up newly-discovered targets without
                # turning on debug logging.
                logger.info(
                    "Proactively created device %s at %s (source=%s, mDNS pending)",
                    device_name, addr, source,
                )
            else:
                dev = self._devices[existing_key]
                # Update IP from address override if not already set from mDNS
                if not dev.ip_address:
                    # DL.3: filled-in-IP path — previously silent.
                    logger.info(
                        "Filled in address for existing device %s: %s (source=%s)",
                        existing_key, addr, source,
                    )
                    dev.ip_address = addr
                # ALWAYS fill in the address source if it's missing — this
                # covers cached devices loaded from /data/device_cache.json
                # (which were saved before address_source was a field) where
                # the IP is already populated but the source is None (#187).
                if dev.address_source is None and source:
                    dev.address_source = source

    @staticmethod
    def _normalize(name: str) -> str:
        """Normalize a device name for comparison (hyphens ↔ underscores).

        ESPHome normalizes device names for mDNS — hyphens become underscores.
        """
        return name.replace("-", "_")

    def _address_override_for(self, device_name: str) -> Optional[str]:
        """Bug #134: hyphen/underscore-tolerant lookup against
        ``_address_overrides``.

        ``build_name_to_target_map`` keys the override by the YAML's
        ``esphome.name`` (typically with hyphens). mDNS announces a
        normalized form (hyphens → underscores). If a device row
        ended up keyed under the mDNS-normalized form before the
        proactive-creation pass ran, ``_address_overrides.get(dev.name)``
        would miss and consumers would fall back to ``dev.ip_address``
        (often a stale ``{name}.local`` from the early-boot fallback).

        Mirrors the normalization already in
        :meth:`_find_existing_device_key` and the encryption-key
        mirroring in :func:`scanner.build_name_to_target_map`.
        """
        if not device_name:
            return None
        override = self._address_overrides.get(device_name)
        if override is not None:
            return override
        norm = self._normalize(device_name)
        for key, value in self._address_overrides.items():
            if self._normalize(key) == norm:
                return value
        return None

    def resolve_ota_address(self, device_name: str) -> Optional[str]:
        """Bug #18 (1.6.1) + #134 (1.7.2): best available address for OTA +
        native-API calls, consolidating the ``_address_overrides.get(name)
        or dev.ip_address`` pattern that was copy-pasted across main.py,
        scheduler.py, and ui_api.py.

        Precedence, strongest-signal first:

        1. ``_address_overrides[name]`` when it's a real IP literal
           (user put a ``use_address`` / ``manual_ip.static_ip`` in
           the YAML — authoritative, always wins).
        2. ``dev.ip_address`` when it's a real IP (mDNS-resolved).
        3. ``_address_overrides[name]`` even if it's a ``.local`` or
           FQDN hostname — used to be the primary path, still a
           better answer than nothing on a LAN where mDNS proxies
           or corporate DNS resolves the name (bug #134).
        4. ``dev.ip_address`` as a last resort (``.local`` fallback).
        5. ``None`` — let the worker fall back to ESPHome's ``--device
           OTA`` sentinel so ESPHome's own resolver runs.

        The bug (radiowave911 at issue #60) was that (1) fell through
        to the ``.local`` fallback when ``_resolve_esphome_config``
        failed during the ESPHome install window — and the
        override-takes-precedence shape then hid the real IP that
        mDNS had since discovered. With this helper, a real IP from
        mDNS beats a stale ``.local`` override every time.

        Logs the resolution at INFO so a future bug report has the
        full waterfall in the add-on log (DL.* discipline, #60).
        """
        dev = self._devices.get(device_name)
        if dev is None:
            mapped_target = self._map_target(device_name)
            if mapped_target is not None:
                dev = next(
                    (d for d in self._devices.values()
                     if d.compile_target == mapped_target),
                    None,
                )
        override = self._address_override_for(device_name)
        dev_ip = dev.ip_address if dev else None
        # Real IP in the override (static_ip / use_address) wins first.
        if override and _is_ip_literal(override):
            resolved = override
            branch = "override_ip_literal"
        # mDNS-resolved real IP is next.
        elif dev_ip and _is_ip_literal(dev_ip):
            resolved = dev_ip
            branch = "dev_ip_literal"
        # Override hostname (use_address FQDN, .local fallback) over
        # dev.ip_address (often a stale .local). Bug #134: corporate
        # FQDNs in use_address must win over the .local fallback.
        elif override:
            resolved = override
            branch = "override_hostname"
        elif dev_ip:
            resolved = dev_ip
            branch = "dev_ip_hostname"
        else:
            resolved = None
            branch = "none"
        logger.debug(
            "resolve_ota_address(%r): override=%r dev_ip=%r → %r (branch=%s)",
            device_name, override, dev_ip, resolved, branch,
        )
        return resolved

    def _map_target(self, device_name: str) -> Optional[str]:
        """Return the YAML filename matching *device_name*, or None.

        Checks the name-to-target map first (covers both explicit
        ``esphome.name`` overrides and filename stems), then falls back
        to a direct filename-stem comparison.  Comparisons are
        hyphen/underscore-insensitive because ESPHome normalizes hyphens
        to underscores in mDNS advertisements.
        """
        norm = self._normalize(device_name)
        if device_name in self._name_to_target:
            return self._name_to_target[device_name]
        # Try normalized lookup
        for key, target in self._name_to_target.items():
            if self._normalize(key) == norm:
                return target
        for target in self._compile_targets:
            stem = Path(target).stem
            if self._normalize(stem) == norm:
                return target
        return None

    def get_devices(self) -> list[Device]:
        return list(self._devices.values())

    async def refresh_target(self, compile_target: str) -> bool:
        """Force an immediate device-info refresh for the device whose
        ``compile_target`` matches *compile_target*.

        Used by the API to push fresh ``running_version``/``compilation_time``
        into the UI right after a successful OTA, instead of waiting up to
        ``poll_interval`` seconds for the next mDNS poll cycle (#11).

        Returns True if a refresh was attempted, False if no matching
        device was found or it has no IP yet.
        """
        async with self._lock:
            target_dev: Optional[Device] = None
            for dev in self._devices.values():
                if dev.compile_target == compile_target:
                    target_dev = dev
                    break
            if target_dev is None or not target_dev.ip_address:
                return False
            name = target_dev.name
            ip = self._address_override_for(name) or target_dev.ip_address
        # Run the query OUTSIDE the lock — it does network I/O.
        await self._query_device(name, ip)
        return True

    async def note_target_flashed(self, compile_target: str) -> bool:
        """#238: stamp ``compilation_time`` server-side after a successful OTA.

        The post-OTA flow used to rely on ``refresh_target`` opening an
        ``aioesphomeapi`` connection to ask the device for the new
        compilation_time. That still works, but we also know the
        compilation moment authoritatively — the firmware *we just
        flashed* was built moments ago — so write it server-side
        immediately. The UI gets a fresh "Last compiled" timestamp
        without depending on the device being reachable in the
        few-second post-reboot window.

        ``running_version`` is left to mDNS / refresh_target — we don't
        always know exactly which ESPHome version compiled the firmware
        without inspecting the bundle, and mDNS TXT updates within
        seconds of the device coming back up.

        Returns True if a Device row was found and stamped.
        """
        # ESPHome reports compilation_time as ISO+offset, e.g.
        # "2026-04-23 06:13:56 -0700". Match that shape so
        # _parse_device_compile_epoch in ui_api.py parses cleanly via
        # the existing "%Y-%m-%d %H:%M:%S %z" format.
        now = datetime.now().astimezone()
        stamped = now.strftime("%Y-%m-%d %H:%M:%S %z")
        async with self._lock:
            target_dev: Optional[Device] = None
            for dev in self._devices.values():
                if dev.compile_target == compile_target:
                    target_dev = dev
                    break
            if target_dev is None:
                return False
            target_dev.compilation_time = stamped
            target_dev.last_seen = _utcnow()
            target_dev.online = True
            self._save_cache()
        return True
