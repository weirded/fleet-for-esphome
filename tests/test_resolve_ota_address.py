"""Tests for the best-OTA-address helper (Bug #18, 1.6.1).

Pins the precedence rules in
``DevicePoller.resolve_ota_address`` — the regression source at issue
#60 was that a stale ``{name}.local`` override was preferred over a
real IP the device poller already knew about (mDNS-resolved), so the
worker shipped with ``--device shopaccesscontrol.local`` and
``esphome upload`` failed inside the worker's Docker container where
mDNS isn't proxied.
"""

from __future__ import annotations

from device_poller import Device, DevicePoller, _is_ip_literal


def _build_poller() -> DevicePoller:
    """Plain DevicePoller without mDNS/zeroconf — we just want the
    helper's pure-function behaviour."""
    return DevicePoller()


def test_is_ip_literal_basic() -> None:
    assert _is_ip_literal("192.168.1.10") is True
    assert _is_ip_literal("fe80::1") is True
    assert _is_ip_literal("shopaccesscontrol.local") is False
    assert _is_ip_literal("not-an-ip") is False
    assert _is_ip_literal("") is False
    assert _is_ip_literal(None) is False  # type: ignore[arg-type]


def test_resolve_prefers_static_ip_override() -> None:
    """The classic happy path: YAML has manual_ip.static_ip, scanner
    resolved cleanly, override holds the literal IP. Always wins."""
    p = _build_poller()
    p._devices = {
        "shopaccesscontrol": Device(
            name="shopaccesscontrol",
            ip_address="192.168.3.50",   # stale mDNS reading
            address_source="mdns",
        ),
    }
    p._address_overrides = {"shopaccesscontrol": "192.168.3.196"}
    p._address_sources = {"shopaccesscontrol": "wifi_static_ip"}

    assert p.resolve_ota_address("shopaccesscontrol") == "192.168.3.196"


def test_resolve_falls_back_to_mdns_ip_when_override_is_local() -> None:
    """Regression for #60: scanner ran during ESPHome install window,
    so the override is the ``{name}.local`` fallback. mDNS discovered
    the real IP afterwards. The helper must pick the real IP, not
    the stale ``.local`` override the worker's container can't
    resolve."""
    p = _build_poller()
    p._devices = {
        "shopaccesscontrol": Device(
            name="shopaccesscontrol",
            ip_address="192.168.3.196",
            address_source="mdns",
        ),
    }
    p._address_overrides = {"shopaccesscontrol": "shopaccesscontrol.local"}
    p._address_sources = {"shopaccesscontrol": "mdns_default"}

    assert p.resolve_ota_address("shopaccesscontrol") == "192.168.3.196"


def test_resolve_uses_local_fallback_when_no_real_ip_known() -> None:
    """All we have is the mDNS-default hostname — still better than
    nothing; LANs with functioning mDNS proxies can resolve it."""
    p = _build_poller()
    p._devices = {
        "device1": Device(
            name="device1",
            ip_address="",
            address_source="mdns_default",
        ),
    }
    p._address_overrides = {"device1": "device1.local"}
    p._address_sources = {"device1": "mdns_default"}

    assert p.resolve_ota_address("device1") == "device1.local"


def test_resolve_returns_none_when_nothing_known() -> None:
    """No override, no ip_address. Worker receives None → uses
    ESPHome's own ``--device OTA`` sentinel."""
    p = _build_poller()
    p._devices = {
        "empty": Device(name="empty", ip_address=""),
    }
    p._address_overrides = {}

    assert p.resolve_ota_address("empty") is None


def test_resolve_handles_missing_device() -> None:
    """Called for a name the poller has never seen. Returns None
    rather than raising."""
    p = _build_poller()
    p._devices = {}
    p._address_overrides = {}

    assert p.resolve_ota_address("nonexistent") is None


def test_resolve_prefers_use_address_fqdn_over_dev_ip() -> None:
    """Bug #134 (1.7.2, robin-thoni): wifi.use_address holds a
    non-``.local`` FQDN routed via corporate DNS. dev.ip_address
    has the same FQDN (proactive creation never got an mDNS IP
    because the device isn't on the local segment). Live Logs +
    the worker were falling back to ``{name}.local`` instead of
    honouring the override."""
    p = _build_poller()
    p._devices = {
        "esp19-btpresence": Device(
            name="esp19-btpresence",
            ip_address="esp19-btpresence.example.com",
            address_source="wifi_use_address",
        ),
    }
    p._address_overrides = {"esp19-btpresence": "esp19-btpresence.example.com"}
    p._address_sources = {"esp19-btpresence": "wifi_use_address"}

    assert p.resolve_ota_address("esp19-btpresence") == "esp19-btpresence.example.com"


def test_resolve_handles_hyphen_underscore_key_mismatch() -> None:
    """Bug #134: if the device row ended up keyed under the
    mDNS-normalized (underscore) form before the proactive YAML
    pass ran, a hyphenated _address_overrides key would miss
    on ``get(dev.name)``. Normalized lookup matches them."""
    p = _build_poller()
    # Device keyed with underscores (as if mDNS announced first).
    p._devices = {
        "esp19_btpresence": Device(
            name="esp19_btpresence",
            ip_address="esp19_btpresence.local",
            address_source="mdns",
        ),
    }
    # YAML-derived override keyed with hyphens.
    p._address_overrides = {"esp19-btpresence": "esp19-btpresence.example.com"}
    p._address_sources = {"esp19-btpresence": "wifi_use_address"}

    # Caller passes the underscore form (dev.name) — normalized lookup
    # must find the hyphenated override.
    assert (
        p.resolve_ota_address("esp19_btpresence")
        == "esp19-btpresence.example.com"
    )


def test_address_override_for_normalized_lookup() -> None:
    """Direct coverage of the private helper used by resolve_ota_address
    and the in-poller fan-out — confirms both spellings resolve to the
    same value without doubling the dict size at the source."""
    p = _build_poller()
    p._address_overrides = {"my-device": "device.example.com"}

    assert p._address_override_for("my-device") == "device.example.com"
    assert p._address_override_for("my_device") == "device.example.com"
    assert p._address_override_for("unknown") is None
    assert p._address_override_for("") is None
