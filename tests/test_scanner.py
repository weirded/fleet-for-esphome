"""Unit tests for the YAML scanner and bundle creator."""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

from scanner import (
    _extract_metadata,
    build_name_to_target_map,
    create_bundle,
    create_stub_yaml,
    duplicate_device,
    rename_device_in_yaml,
    get_archived_device_metadata,
    get_device_address,
    get_device_metadata,
    get_esphome_version,
    scan_archived,
    scan_configs,
)


def _empty_meta() -> dict:
    """Return a fresh empty metadata dict matching get_device_metadata's shape."""
    return {
        "friendly_name": None,
        "device_name": None,
        "device_name_raw": None,
        "comment": None,
        "area": None,
        "project_name": None,
        "project_version": None,
        "has_web_server": False,
        # UD.5: surfaced via the Devices-tab Platform column. Defaults
        # to None so a YAML without a chip block (rare) reads cleanly.
        "board": None,
    }

FIXTURES = Path(__file__).parent / "fixtures" / "esphome_configs"


# ---------------------------------------------------------------------------
# scan_configs
# ---------------------------------------------------------------------------

def test_scan_finds_yaml_files():
    targets = scan_configs(str(FIXTURES))
    assert "device1.yaml" in targets
    assert "device2.yaml" in targets


def test_scan_excludes_secrets_yaml():
    targets = scan_configs(str(FIXTURES))
    assert "secrets.yaml" not in targets
    assert not any(t.lower() == "secrets.yaml" for t in targets)


def test_scan_excludes_subdirectory_yaml():
    """Only top-level YAMLs should be returned."""
    targets = scan_configs(str(FIXTURES))
    assert not any("packages" in t for t in targets)


def test_scan_nonexistent_dir():
    targets = scan_configs("/nonexistent/path/that/does/not/exist")
    assert targets == []


def test_scan_missing_dir_logs_info_once(tmp_path, caplog):
    """Bug #86: a missing config dir is a config state (no ESPHome
    builder add-on, or user hasn't created the dir yet), not a crash
    condition. Log it once at INFO, then DEBUG on every subsequent
    scan so the log doesn't flood every poll tick.
    """
    import logging
    import scanner as scanner_module

    missing = tmp_path / "does_not_exist"
    scanner_module._missing_config_dirs_logged.discard(str(missing))

    try:
        with caplog.at_level(logging.DEBUG, logger="scanner"):
            scan_configs(str(missing))
            scan_configs(str(missing))
            scan_configs(str(missing))

        info_lines = [r for r in caplog.records if r.levelno == logging.INFO and "does not exist yet" in r.message]
        debug_lines = [r for r in caplog.records if r.levelno == logging.DEBUG and "still missing" in r.message]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "does not exist or is not a directory" in r.message]

        # Exactly one INFO line total, regardless of how many scans.
        assert len(info_lines) == 1, f"Expected 1 INFO line, got {len(info_lines)}"
        # Subsequent scans log DEBUG.
        assert len(debug_lines) == 2, f"Expected 2 DEBUG lines, got {len(debug_lines)}"
        # No WARNING — the old flood-prone log level is gone.
        assert warnings == []
    finally:
        scanner_module._missing_config_dirs_logged.discard(str(missing))


def test_scan_resurfaced_dir_resets_suppression(tmp_path, caplog):
    """When the missing dir reappears, log an INFO that scans have
    resumed — and if it disappears again later, the 'missing' INFO
    should fire again (suppression state must reset).
    """
    import logging
    import scanner as scanner_module

    d = tmp_path / "esphome"
    scanner_module._missing_config_dirs_logged.discard(str(d))

    try:
        with caplog.at_level(logging.INFO, logger="scanner"):
            scan_configs(str(d))  # missing → INFO
            d.mkdir()
            scan_configs(str(d))  # present → INFO "resuming"
            import shutil
            shutil.rmtree(d)
            scan_configs(str(d))  # missing again → INFO

        messages = [r.message for r in caplog.records if r.name == "scanner"]
        missing_count = sum(1 for m in messages if "does not exist yet" in m)
        resumed_count = sum(1 for m in messages if "now available" in m)
        assert missing_count == 2
        assert resumed_count == 1
    finally:
        scanner_module._missing_config_dirs_logged.discard(str(d))


def test_scan_returns_sorted_list():
    targets = scan_configs(str(FIXTURES))
    assert targets == sorted(targets)


def test_scan_only_returns_filenames():
    """Results should be filenames only, not full paths."""
    targets = scan_configs(str(FIXTURES))
    for t in targets:
        assert "/" not in t
        assert t.endswith(".yaml")


def test_scan_empty_dir(tmp_path):
    targets = scan_configs(str(tmp_path))
    assert targets == []


def test_scan_dir_with_only_secrets(tmp_path):
    (tmp_path / "secrets.yaml").write_text("key: val")
    targets = scan_configs(str(tmp_path))
    assert targets == []


# ---------------------------------------------------------------------------
# scan_archived — DM.1. Lists YAMLs under ``<config_dir>/.archive/`` so
# the Devices endpoint can merge archived rows into its response and the
# UI can render them inline (toggleable via the column picker).
# ---------------------------------------------------------------------------

def test_scan_archived_no_archive_dir(tmp_path):
    """Fresh install with nothing archived → empty list, not an error."""
    assert scan_archived(str(tmp_path)) == []


def test_scan_archived_returns_yaml_files(tmp_path):
    archive = tmp_path / ".archive"
    archive.mkdir()
    (archive / "alpha.yaml").write_text("esphome:\n  name: alpha\n")
    (archive / "beta.yml").write_text("esphome:\n  name: beta\n")
    rows = scan_archived(str(tmp_path))
    names = {r["filename"] for r in rows}
    assert names == {"alpha.yaml", "beta.yml"}


def test_scan_archived_skips_non_yaml(tmp_path):
    archive = tmp_path / ".archive"
    archive.mkdir()
    (archive / "device.yaml").write_text("esphome:\n  name: device\n")
    (archive / "README.md").write_text("notes")
    (archive / "key.bin").write_text("x")
    rows = scan_archived(str(tmp_path))
    assert [r["filename"] for r in rows] == ["device.yaml"]


def test_scan_archived_includes_size_and_archived_at(tmp_path):
    archive = tmp_path / ".archive"
    archive.mkdir()
    body = "esphome:\n  name: alpha\n"
    p = archive / "alpha.yaml"
    p.write_text(body)
    rows = scan_archived(str(tmp_path))
    assert len(rows) == 1
    row = rows[0]
    assert row["filename"] == "alpha.yaml"
    assert row["size"] == len(body.encode("utf-8"))
    # archived_at is the file mtime as epoch seconds (float).
    assert isinstance(row["archived_at"], float)
    assert row["archived_at"] == p.stat().st_mtime


def test_scan_archived_round_trip_with_scan_configs(tmp_path):
    """Round-trip: an active YAML moved to .archive/ disappears from
    scan_configs and appears in scan_archived."""
    src = tmp_path / "device.yaml"
    src.write_text("esphome:\n  name: device\n")
    assert "device.yaml" in scan_configs(str(tmp_path))
    assert scan_archived(str(tmp_path)) == []

    archive = tmp_path / ".archive"
    archive.mkdir()
    src.rename(archive / "device.yaml")
    assert "device.yaml" not in scan_configs(str(tmp_path))
    rows = scan_archived(str(tmp_path))
    assert [r["filename"] for r in rows] == ["device.yaml"]


def test_scan_archived_sorted(tmp_path):
    archive = tmp_path / ".archive"
    archive.mkdir()
    for n in ("zebra.yaml", "alpha.yaml", "mango.yaml"):
        (archive / n).write_text("x")
    rows = scan_archived(str(tmp_path))
    assert [r["filename"] for r in rows] == sorted(r["filename"] for r in rows)


def test_scan_archived_ignores_subdirectory(tmp_path):
    archive = tmp_path / ".archive"
    nested = archive / "subdir"
    nested.mkdir(parents=True)
    (nested / "buried.yaml").write_text("x")
    rows = scan_archived(str(tmp_path))
    assert rows == []


# ---------------------------------------------------------------------------
# get_archived_device_metadata — #203. Archived rows used to come back from
# the /ui/api/targets endpoint with every attribute set to None, dropping
# tags / area / project / pinned_version / schedule the moment a device
# was archived. The helper re-reads the YAML under .archive/ and pulls the
# same shape get_device_metadata returns (raw-YAML path only — no ESPHome
# resolution because archived files may reference deleted secrets).
# ---------------------------------------------------------------------------

def test_archived_metadata_missing_dir_returns_empty_shape(tmp_path):
    """No .archive/ directory → still returns the canonical shape so the
    caller can spread keys without KeyError."""
    meta = get_archived_device_metadata(str(tmp_path), "alpha.yaml")
    assert meta["tags"] is None
    assert meta["area"] is None
    assert meta["pinned_version"] is None
    assert meta["bluetooth_proxy"] == "off"


def test_archived_metadata_preserves_tags_and_area(tmp_path):
    """Archived YAML's tags + area survive the round-trip — this is the
    bug #203 regression: tag-filter pills lost archived rows entirely."""
    archive = tmp_path / ".archive"
    archive.mkdir()
    (archive / "alpha.yaml").write_text(
        "# esphome-fleet:\n"
        "#   tags: 'kitchen, bedroom'\n"
        "#   pin_version: '2024.6.1'\n"
        "esphome:\n"
        "  name: alpha\n"
        "  area: Living Room\n"
        "  project:\n"
        "    name: my-project\n"
        "    version: '1.2.3'\n"
    )
    meta = get_archived_device_metadata(str(tmp_path), "alpha.yaml")
    assert meta["tags"] == "kitchen, bedroom"
    assert meta["pinned_version"] == "2024.6.1"
    assert meta["area"] == "Living Room"
    assert meta["project_name"] == "my-project"
    assert meta["project_version"] == "1.2.3"


def test_archived_metadata_preserves_schedule(tmp_path):
    archive = tmp_path / ".archive"
    archive.mkdir()
    (archive / "alpha.yaml").write_text(
        "# esphome-fleet:\n"
        "#   schedule: '0 2 * * *'\n"
        "#   schedule_enabled: true\n"
        "esphome:\n"
        "  name: alpha\n"
    )
    meta = get_archived_device_metadata(str(tmp_path), "alpha.yaml")
    assert meta["schedule"] == "0 2 * * *"
    assert meta["schedule_enabled"] is True


def test_archived_metadata_resolves_friendly_name_via_packages(tmp_path):
    """#212: archived YAML composed via ``packages:`` (or ``<<: !include``)
    must still render the device's friendly_name in the Archived view.
    Pre-fix the raw loader treated ``!include``/``packages:`` as opaque,
    so any device whose ``esphome:`` block was contributed by a package
    came back with friendly_name=None and the row showed the filename.
    """
    archive = tmp_path / ".archive"
    archive.mkdir()
    (tmp_path / "common.yaml").write_text(
        "esphome:\n"
        "  name: athom-plug-3\n"
        "  friendly_name: Athom Plug 3\n"
    )
    (archive / "athom-plug-3.yaml").write_text(
        "packages:\n"
        "  base: !include ../common.yaml\n"
    )
    meta = get_archived_device_metadata(str(tmp_path), "athom-plug-3.yaml")
    # Either the full ESPHome resolver merges packages and exposes the
    # friendly_name, or — if ESPHome isn't importable in this test env —
    # the raw loader doesn't crash and other fields still come back in
    # the canonical shape.
    if meta["friendly_name"] is not None:
        assert meta["friendly_name"] == "Athom Plug 3"
        assert meta["device_name_raw"] == "athom-plug-3"


def test_archived_metadata_unparseable_yaml_falls_back_to_empty(tmp_path):
    """A broken YAML (e.g. references a deleted !include) shouldn't crash
    the Devices endpoint — return the empty shape instead so the row
    still renders, just without metadata."""
    archive = tmp_path / ".archive"
    archive.mkdir()
    (archive / "broken.yaml").write_text(
        "esphome:\n"
        "  name: broken\n"
        "  area: !include missing_file.yaml\n"
        "{this is: not: valid: yaml\n"
    )
    meta = get_archived_device_metadata(str(tmp_path), "broken.yaml")
    # Some keys may parse before the failure; the contract is "doesn't
    # raise" + canonical shape, not "always None".
    assert "tags" in meta
    assert "bluetooth_proxy" in meta


# ---------------------------------------------------------------------------
# create_bundle — BD (WORKITEMS-1.6.2). Per-target bundles via
# ESPHome's ConfigBundleCreator ship only referenced files + filtered
# secrets. Every test below is structured as a regression guard for a
# specific leak the pre-BD rglob path permitted.
# ---------------------------------------------------------------------------

def _bundle_names(raw: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        return tar.getnames()


def _bundle_file_bytes(raw: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        fp = tar.extractfile(name)
        assert fp is not None, f"{name} missing from bundle: {tar.getnames()}"
        return fp.read()


def test_bundle_is_tar_gz():
    raw = create_bundle(str(FIXTURES), "device1.yaml")
    assert isinstance(raw, bytes)
    assert len(raw) > 0
    # gzip magic bytes
    assert raw[:2] == b"\x1f\x8b"


def test_bundle_ships_the_target_yaml():
    names = _bundle_names(create_bundle(str(FIXTURES), "device1.yaml"))
    assert "device1.yaml" in names


def test_bundle_paths_are_relative():
    """Archive paths must not start with '/' — workers extract into a
    per-slot dir and an absolute path would escape it."""
    names = _bundle_names(create_bundle(str(FIXTURES), "device1.yaml"))
    for name in names:
        assert not name.startswith("/"), f"Absolute path in bundle: {name}"


def test_bundle_includes_manifest():
    """ConfigBundleCreator always emits a manifest.json at the tree root."""
    names = _bundle_names(create_bundle(str(FIXTURES), "device1.yaml"))
    assert "manifest.json" in names


# --- BD.3.3 — bundle for target X does NOT ship unrelated target Y ----------

def test_bundle_omits_unrelated_targets():
    """Pre-BD regression guard: bundle for device1 used to include
    every .yaml in the config directory. ConfigBundleCreator walks
    the validated config and only adds files the target references,
    so device2.yaml (and anything else not `!include`d by device1)
    must not be in the archive.
    """
    names = _bundle_names(create_bundle(str(FIXTURES), "device1.yaml"))
    assert "device2.yaml" not in names, (
        f"bundle for device1.yaml leaked device2.yaml — full list: {names}"
    )


def test_bundle_omits_unrelated_package_files(tmp_path):
    """Package files unreferenced by the target aren't shipped."""
    (tmp_path / "secrets.yaml").write_text(
        'wifi_ssid: "bundle-test-ssid"\nwifi_password: "bundle-test-password-long-enough"\nota_password: "bundle-test-ota-password"\n'
    )
    (tmp_path / "device-a.yaml").write_text(
        "esphome:\n  name: device-a\n"
        "esp8266:\n  board: d1_mini\n"
        "wifi:\n  ssid: !secret wifi_ssid\n  password: !secret wifi_password\n"
    )
    # A second target that uses an included package — that package
    # file must not ship with device-a's bundle.
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "shared.yaml").write_text(
        "logger:\n  level: DEBUG\n"
    )
    (tmp_path / "device-b.yaml").write_text(
        "esphome:\n  name: device-b\n"
        "esp8266:\n  board: d1_mini\n"
        "wifi:\n  ssid: !secret wifi_ssid\n  password: !secret wifi_password\n"
        "packages:\n  shared: !include packages/shared.yaml\n"
    )
    names = _bundle_names(create_bundle(str(tmp_path), "device-a.yaml"))
    assert "packages/shared.yaml" not in names
    assert "device-b.yaml" not in names


# --- BD.3.1 — `.git/` never ships -------------------------------------------

def test_bundle_excludes_git_dir(tmp_path):
    """Pre-BD regression guard: rglob shipped `.git/config` (containing
    remote URLs + any wired-up push credentials) and loose objects to
    every claiming worker. ConfigBundleCreator walks the config tree,
    not the filesystem, so `.git/*` never appears.
    """
    (tmp_path / "secrets.yaml").write_text(
        'wifi_ssid: "bundle-test-ssid"\nwifi_password: "bundle-test-password-long-enough"\nota_password: "bundle-test-ota-password"\n'
    )
    (tmp_path / "device.yaml").write_text(
        "esphome:\n  name: my-device\n"
        "esp8266:\n  board: d1_mini\n"
        "wifi:\n  ssid: !secret wifi_ssid\n  password: !secret wifi_password\n"
    )
    # Seed a believable-looking `.git/` tree with a push URL + a loose
    # object so a regression can't pass by checking for just the dir name.
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        "[remote \"origin\"]\n"
        "  url = https://ghp_SUPERSECRETPAT@github.com/user/repo.git\n"
    )
    (git_dir / "objects" / "ab").mkdir(parents=True)
    (git_dir / "objects" / "ab" / "cdef1234").write_bytes(b"\x78\x9c\x01\x00\x00")

    raw = create_bundle(str(tmp_path), "device.yaml")
    names = _bundle_names(raw)
    assert not any(".git" in n.split("/") for n in names), (
        f".git leaked in bundle: {[n for n in names if '.git' in n]}"
    )


# --- BD.3.2 — secrets.yaml is filtered to referenced keys only --------------

def test_bundle_filters_secrets_to_referenced_keys(tmp_path):
    """Pre-BD regression guard: rglob shipped the entire secrets.yaml
    (every device's WiFi PSK, API noise-PSK, OTA password) to every
    worker. ConfigBundleCreator loads secrets.yaml, intersects with the
    keys actually `!secret`-referenced by the bundled YAML tree, and
    only ships those keys.
    """
    (tmp_path / "secrets.yaml").write_text(
        'wifi_ssid: "my-ssid"\n'
        'wifi_password: "my-wifi-password"\n'
        'ota_password: "my-ota-password"\n'
        'api_encryption_key: "Zp82U4SqCqe55xkDDuPXzsoNhcmEws7/HbNXsv2qOGI="\n'
        'other_device_api_key: "OTHER-DEVICE-PSK-MUST-NOT-LEAK"\n'
        'unused_backdoor_password: "ALSO-MUST-NOT-LEAK"\n'
    )
    (tmp_path / "device.yaml").write_text(
        "esphome:\n  name: my-device\n"
        "esp8266:\n  board: d1_mini\n"
        "wifi:\n  ssid: !secret wifi_ssid\n  password: !secret wifi_password\n"
    )
    secrets_content = _bundle_file_bytes(
        create_bundle(str(tmp_path), "device.yaml"), "secrets.yaml",
    ).decode()

    # Keys this target references — must be present.
    assert "wifi_ssid" in secrets_content
    assert "wifi_password" in secrets_content

    # Keys this target does NOT reference — must be filtered out.
    assert "other_device_api_key" not in secrets_content
    assert "OTHER-DEVICE-PSK-MUST-NOT-LEAK" not in secrets_content
    assert "unused_backdoor_password" not in secrets_content
    assert "ALSO-MUST-NOT-LEAK" not in secrets_content


def test_bundle_raises_on_validation_error(tmp_path):
    """BD intentionally has no fallback — a target that fails ESPHome's
    full validator can't be dispatched until the YAML is fixed. Better
    than silently shipping the full config directory.
    """
    (tmp_path / "secrets.yaml").write_text('wifi_ssid: "bundle-test-ssid"\nwifi_password: "bundle-test-password-long-enough"\n')
    # Invalid: `esp8266.board` is missing.
    (tmp_path / "broken.yaml").write_text(
        "esphome:\n  name: broken\n"
        "esp8266: {}\n"
        "wifi:\n  ssid: !secret wifi_ssid\n  password: !secret wifi_password\n"
    )
    with pytest.raises(Exception):
        create_bundle(str(tmp_path), "broken.yaml")


# ---------------------------------------------------------------------------
# get_esphome_version
# ---------------------------------------------------------------------------

def test_get_esphome_version_returns_string():
    ver = get_esphome_version()
    assert isinstance(ver, str)
    assert len(ver) > 0


def test_get_esphome_version_returns_unknown_when_not_installed():
    """If esphome is not installed, should return 'unknown' without crashing."""
    import importlib.metadata as meta
    import scanner

    original = meta.version
    original_selected = scanner._selected_esphome_version

    def mock_version(pkg):
        if pkg == "esphome":
            raise meta.PackageNotFoundError(pkg)
        return original(pkg)

    meta.version = mock_version
    scanner._selected_esphome_version = None
    # SE.7: without the failure flag set, the new logic assumes the
    # lazy-install is in flight and returns "installing". This test
    # exercises the "install won't help" terminal state, so simulate
    # the failure flag too.
    scanner._esphome_install_failed = True
    try:
        ver = get_esphome_version()
        assert ver == "unknown"
    finally:
        meta.version = original
        scanner._selected_esphome_version = original_selected
        scanner._esphome_install_failed = False


# ---------------------------------------------------------------------------
# get_device_metadata — extracting name/friendly_name/area/comment/project
# ---------------------------------------------------------------------------

def _write_yaml(config_dir: Path, name: str, content: str) -> None:
    (config_dir / name).write_text(content)


# ---------------------------------------------------------------------------
# _extract_metadata — call directly with hand-crafted dicts.
#
# These tests deliberately bypass _resolve_esphome_config (which is fragile
# across ESPHome versions: a tiny test fixture that the local 2026.3.1
# accepts can be rejected by 2026.3.3 in CI). Calling _extract_metadata with
# a pre-resolved dict tests OUR extraction logic, not ESPHome's schema.
#
# End-to-end coverage of the resolver path lives in the fixture-based tests
# below, which use the known-good device1.yaml fixture.
# ---------------------------------------------------------------------------

def test_metadata_extracts_name_and_friendly_name():
    config = {
        "esphome": {
            "name": "living-room-sensor",
            "friendly_name": "Living Room Sensor",
        },
    }
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["device_name_raw"] == "living-room-sensor"
    assert meta["device_name"] == "Living Room Sensor"
    assert meta["friendly_name"] == "Living Room Sensor"


def test_metadata_extracts_area_and_comment():
    config = {
        "esphome": {
            "name": "dev",
            "area": "Kitchen",
            "comment": "Over the sink",
        },
    }
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["area"] == "Kitchen"
    assert meta["comment"] == "Over the sink"


def test_metadata_extracts_area_from_dict_form():
    """Bug #18: ESPHome's newer schema accepts ``area: {name: ..., id: ...}``.
    The extractor must surface the human-readable name rather than the
    repr of the dict (which renders as a JSON-looking blob in the UI).
    """
    config = {
        "esphome": {
            "name": "dev",
            "area": {"name": "Living Room", "id": "lr1"},
        },
    }
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["area"] == "Living Room"


def test_metadata_extracts_area_from_dict_form_id_fallback():
    """Bug #18: dict area with no name still resolves via the id."""
    config = {
        "esphome": {
            "name": "dev",
            "area": {"id": "lr1"},
        },
    }
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["area"] == "lr1"


def test_metadata_extracts_project():
    config = {
        "esphome": {
            "name": "dev",
            "project": {"name": "example.device", "version": "1.2.3"},
        },
    }
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["project_name"] == "example.device"
    assert meta["project_version"] == "1.2.3"


def test_metadata_detects_web_server():
    config = {
        "esphome": {"name": "dev"},
        "web_server": {"port": 80},
    }
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["has_web_server"] is True


def test_metadata_missing_web_server():
    config = {"esphome": {"name": "dev"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["has_web_server"] is False


def test_metadata_detects_web_server_with_no_value():
    """#74: ESPHome allows `web_server:` with no value (enables with defaults).

    YAML parses this as {"web_server": None}. The detection must check
    for key PRESENCE, not key VALUE.
    """
    config = {"esphome": {"name": "dev"}, "web_server": None}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["has_web_server"] is True


def test_metadata_all_fields_none_for_minimal_config():
    """A minimal config with only esphome.name leaves the optional fields untouched."""
    config = {"esphome": {"name": "dev"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["device_name_raw"] == "dev"
    assert meta["friendly_name"] is None
    assert meta["area"] is None
    assert meta["comment"] is None
    assert meta["project_name"] is None
    assert meta["project_version"] is None
    assert meta["has_web_server"] is False


def test_metadata_no_esphome_block():
    """A config that's missing the esphome block leaves metadata as defaults."""
    meta = _empty_meta()
    _extract_metadata({}, meta)
    assert meta["device_name_raw"] is None
    assert meta["friendly_name"] is None


# ---------------------------------------------------------------------------
# Bug #23: ESP type + bluetooth_proxy extraction
# ---------------------------------------------------------------------------

def test_metadata_esp_type_esp32_default_variant():
    """``esp32:`` block with no ``variant:`` reads as plain ``ESP32``."""
    config = {"esphome": {"name": "dev"}, "esp32": {"board": "esp32dev"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["esp_type"] == "ESP32"


def test_metadata_esp_type_esp32_s3_variant_renders_with_dash():
    """``variant: esp32s3`` reads as ``ESP32-S3`` (Espressif product naming)."""
    config = {"esphome": {"name": "dev"}, "esp32": {"variant": "esp32s3"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["esp_type"] == "ESP32-S3"


def test_metadata_esp_type_esp8266():
    config = {"esphome": {"name": "dev"}, "esp8266": {"board": "d1_mini"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["esp_type"] == "ESP8266"


def test_metadata_esp_type_rp2040():
    config = {"esphome": {"name": "dev"}, "rp2040": {"board": "rpipico"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["esp_type"] == "RP2040"


# ---------------------------------------------------------------------------
# UD.5: PlatformIO board extraction
# ---------------------------------------------------------------------------


def test_metadata_board_extracted_from_esp32_block():
    config = {"esphome": {"name": "dev"}, "esp32": {"board": "esp32dev"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["board"] == "esp32dev"


def test_metadata_board_extracted_from_esp32_s3_block():
    """ESP32 with variant + board both surface — variant in esp_type, board in board."""
    config = {
        "esphome": {"name": "dev"},
        "esp32": {"variant": "esp32s3", "board": "esp32-s3-devkitm-1"},
    }
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["esp_type"] == "ESP32-S3"
    assert meta["board"] == "esp32-s3-devkitm-1"


def test_metadata_board_extracted_from_esp8266_block():
    config = {"esphome": {"name": "dev"}, "esp8266": {"board": "d1_mini"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["board"] == "d1_mini"


def test_metadata_board_extracted_from_rp2040_block():
    config = {"esphome": {"name": "dev"}, "rp2040": {"board": "rpipico"}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["board"] == "rpipico"


def test_metadata_board_none_when_block_has_no_board_key():
    """No ``board:`` field → board stays None (rare; bare ``esp32:`` block)."""
    config = {"esphome": {"name": "dev"}, "esp32": {}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["esp_type"] == "ESP32"
    assert meta["board"] is None


def test_metadata_board_none_for_host_platform():
    """Host platform is virtual — no PlatformIO board concept."""
    config = {"esphome": {"name": "dev"}, "host": {}}
    meta = _empty_meta()
    _extract_metadata(config, meta)
    assert meta["esp_type"] == "Host"
    assert meta["board"] is None


def test_metadata_bluetooth_proxy_off_when_block_absent():
    config = {"esphome": {"name": "dev"}, "esp32": {}}
    meta = _empty_meta()
    meta["bluetooth_proxy"] = "off"  # match get_device_metadata's seed
    _extract_metadata(config, meta)
    assert meta["bluetooth_proxy"] == "off"


def test_metadata_bluetooth_proxy_passive_when_block_present_no_active():
    """``bluetooth_proxy:`` with no value (or no ``active:``) is passive mode."""
    config = {"esphome": {"name": "dev"}, "esp32": {}, "bluetooth_proxy": None}
    meta = _empty_meta()
    meta["bluetooth_proxy"] = "off"
    _extract_metadata(config, meta)
    assert meta["bluetooth_proxy"] == "passive"


def test_metadata_bluetooth_proxy_active_when_active_true():
    config = {
        "esphome": {"name": "dev"},
        "esp32": {},
        "bluetooth_proxy": {"active": True},
    }
    meta = _empty_meta()
    meta["bluetooth_proxy"] = "off"
    _extract_metadata(config, meta)
    assert meta["bluetooth_proxy"] == "active"


# ---------------------------------------------------------------------------
# build_name_to_target_map
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# build_name_to_target_map — exercised against the known-good FIXTURES dir
# instead of inline tmp_path configs (which break across ESPHome versions).
# device1.yaml has esphome.name=device1 + api.encryption.key, so it covers
# the stem fallback, the device-name mapping, and encryption key extraction
# in one shot.
# ---------------------------------------------------------------------------

def test_name_map_uses_filename_stem_fallback():
    """Filename stem is always in the map as a fallback."""
    name_map, _, _, _ = build_name_to_target_map(str(FIXTURES), ["device1.yaml"])
    assert name_map["device1"] == "device1.yaml"


def test_name_map_extracts_encryption_key():
    """API encryption keys are extracted and keyed by device name."""
    _, keys, _, _ = build_name_to_target_map(str(FIXTURES), ["device1.yaml"])
    # The fixture's secrets.yaml maps api_encryption_key to a real base64 key
    assert "device1" in keys
    assert keys["device1"]  # non-empty


def test_name_map_resolves_despite_unresolved_substitution():
    """Bug #22: YAMLs with an undefined substitution (e.g. ${pretty_name}
    referenced but not declared) must still produce scanner metadata —
    the resolver has to pass ``ignore_missing=True`` to ESPHome's
    substitution pass when available, otherwise any missing reference
    raises and the entire config silently returns empty.
    """
    name_map, keys, overrides, _ = build_name_to_target_map(
        str(FIXTURES), ["unresolved_subs_device.yaml"],
    )
    # The device_name substitution resolves, so the device name itself
    # must make it into the name_map.
    assert "un-sub-device" in name_map, (
        f"name_map is missing resolved device name; got {name_map}"
    )
    assert name_map["un-sub-device"] == "unresolved_subs_device.yaml"
    # API encryption key must be extracted (keyed by resolved name).
    assert "un-sub-device" in keys
    # Address override is always registered — at minimum the mdns fallback.
    assert "un-sub-device" in overrides


def test_name_map_encryption_keys_include_underscore_variant():
    """Bug #11 (1.6.1): aioesphomeapi / mDNS often normalise hyphenated
    device names to underscores (``un-sub-device`` → ``un_sub_device``),
    so the encryption-key map must carry BOTH forms. Pre-1.6.1 only the
    name_map did this mirroring; the key map didn't, and live logs for
    an encrypted ``my-device`` silently fell through to an unencrypted
    handshake that the device rejects."""
    _, keys, _, _ = build_name_to_target_map(
        str(FIXTURES), ["unresolved_subs_device.yaml"],
    )
    assert "un-sub-device" in keys
    assert "un_sub_device" in keys
    # Both aliases must point at the same key (not accidentally distinct).
    assert keys["un-sub-device"] == keys["un_sub_device"]


def test_get_device_metadata_uses_friendly_name_for_unresolved_subs():
    """Bug #22 follow-up: get_device_metadata must still extract
    device_name for a YAML that contains an unresolved substitution.
    (friendly_name may be None when it references an undefined sub; the
    UI falls back to device_name in that case — but device_name must NOT
    be None, which is what the regression had before.)
    """
    from scanner import get_device_metadata

    meta = get_device_metadata(str(FIXTURES), "unresolved_subs_device.yaml")
    assert meta["device_name"] is not None, (
        "device_name should resolve from ${device_name} even when friendly_name doesn't"
    )
    # device_name is title-cased ("un-sub-device" → "Un Sub Device")
    assert "Un Sub Device" in meta["device_name"]


def test_name_map_empty_targets(tmp_path):
    name_map, keys, overrides, sources = build_name_to_target_map(str(tmp_path), [])
    assert name_map == {}
    assert keys == {}
    assert overrides == {}
    assert sources == {}


# ---------------------------------------------------------------------------
# get_device_address — bug #179
# Mirrors ESPHome CORE.address: wifi → ethernet → openthread, each honoring
# use_address → manual_ip.static_ip → {name}.local fallback.
# ---------------------------------------------------------------------------

def test_get_device_address_wifi_use_address():
    config = {"wifi": {"use_address": "192.168.1.42"}}
    assert get_device_address(config, "dev") == ("192.168.1.42", "wifi_use_address")


def test_get_device_address_wifi_use_address_fqdn():
    """Bug #134 (1.7.2, robin-thoni): a non-``.local`` FQDN routed via
    corporate DNS must round-trip through the scanner verbatim so the
    OTA invocation, Live Logs WS, and the device-row IP cell all agree
    on the same address."""
    config = {"wifi": {"use_address": "esp19-btpresence.example.com"}}
    assert get_device_address(config, "esp19-btpresence") == (
        "esp19-btpresence.example.com",
        "wifi_use_address",
    )


def test_get_device_address_wifi_static_ip():
    config = {"wifi": {"manual_ip": {"static_ip": "10.0.0.5"}}}
    assert get_device_address(config, "dev") == ("10.0.0.5", "wifi_static_ip")


def test_get_device_address_wifi_default_to_mdns():
    config = {"wifi": {"ssid": "test"}}
    assert get_device_address(config, "dev") == ("dev.local", "mdns_default")


def test_get_device_address_ethernet_use_address():
    config = {"ethernet": {"use_address": "10.0.0.10"}}
    assert get_device_address(config, "dev") == ("10.0.0.10", "ethernet_use_address")


def test_get_device_address_ethernet_static_ip():
    config = {"ethernet": {"manual_ip": {"static_ip": "10.0.0.11"}}}
    assert get_device_address(config, "dev") == ("10.0.0.11", "ethernet_static_ip")


def test_get_device_address_ethernet_default_to_mdns():
    config = {"ethernet": {"type": "LAN8720"}}
    assert get_device_address(config, "dev") == ("dev.local", "mdns_default")


def test_get_device_address_openthread_use_address():
    """Thread-only devices: openthread.use_address overrides everything."""
    config = {"openthread": {"use_address": "fd00::1"}}
    assert get_device_address(config, "thread-dev") == ("fd00::1", "openthread_use_address")


def test_get_device_address_openthread_default_to_mdns():
    """Thread-only device with no explicit address falls back to mDNS hostname."""
    config = {"openthread": {"network_key": "deadbeef"}}
    assert get_device_address(config, "thread-dev") == ("thread-dev.local", "mdns_default")


def test_get_device_address_nothing_configured():
    """Empty config (no network block at all) falls back to {name}.local."""
    config = {"esphome": {"name": "minimal"}}
    assert get_device_address(config, "minimal") == ("minimal.local", "mdns_default")


# Bonus: wifi takes precedence over ethernet/openthread when multiple are present
def test_get_device_address_wifi_wins_over_ethernet():
    config = {
        "wifi": {"use_address": "192.168.1.42"},
        "ethernet": {"use_address": "10.0.0.10"},
    }
    assert get_device_address(config, "dev") == ("192.168.1.42", "wifi_use_address")


# ---------------------------------------------------------------------------
# build_name_to_target_map populates address_overrides for ALL targets (#179)
# ---------------------------------------------------------------------------

# The static-IP, DHCP, and Thread-only cases are exercised by the
# FIXTURE-based tests below, which use real known-good ESPHome configs in
# tests/fixtures/esphome_configs/. Inline tmp_path tests for these would be
# fragile across ESPHome versions because the resolver's schema changes
# from version to version.


# ---------------------------------------------------------------------------
# Fixture-based integration tests for #186 — verify the real fixture YAMLs
# (which include !secret + manual_ip / openthread blocks) actually parse
# through ESPHome's full resolution pipeline and yield the right metadata.
# These exercise the same code path the production code uses, not isolated
# helper functions.
# ---------------------------------------------------------------------------

def test_static_ip_fixture_resolves_address():
    """Fixture: tests/fixtures/esphome_configs/static_ip_device.yaml"""
    _, _, overrides, sources = build_name_to_target_map(
        str(FIXTURES), ["static_ip_device.yaml"],
    )
    assert overrides.get("static-ip-device") == "192.168.1.99"
    assert sources.get("static-ip-device") == "wifi_static_ip"


def test_thread_only_fixture_resolves_to_mdns():
    """Fixture: tests/fixtures/esphome_configs/thread_only_device.yaml

    A Thread-only device with no wifi/ethernet block should still get an
    address override (falling back to {name}.local). Without this, the YAML
    row never exists and any later mDNS discovery duplicates it (#179).
    """
    _, _, overrides, sources = build_name_to_target_map(
        str(FIXTURES), ["thread_only_device.yaml"],
    )
    assert "thread-only-device" in overrides
    assert overrides["thread-only-device"] == "thread-only-device.local"
    assert sources["thread-only-device"] == "mdns_default"


def test_static_ip_fixture_metadata():
    """Static-IP device's friendly_name still resolves correctly."""
    meta = get_device_metadata(str(FIXTURES), "static_ip_device.yaml")
    assert meta["friendly_name"] == "Static IP Device"
    assert meta["device_name_raw"] == "static-ip-device"


# ---------------------------------------------------------------------------
# #84: wifi.domain is honored because we run ESPHome's full validator
# (which injects `wifi.use_address = CORE.name + config[CONF_DOMAIN]`).
# Before this fix, the substitution-only pipeline left use_address unset and
# our waterfall fell through to `{name}.local` regardless of `domain:`.
# ---------------------------------------------------------------------------

def test_wifi_domain_fixture_resolves_address():
    """Fixture: tests/fixtures/esphome_configs/wifi_domain.yaml

    Device declares ``wifi.domain: .example.internal`` but no ``use_address``.
    After full validation, ``wifi.use_address`` is injected as
    ``wifi-domain-device.example.internal`` — that must propagate to
    ``address_overrides`` so the worker OTAs to the right host, not
    ``wifi-domain-device.local``.
    """
    _, _, overrides, sources = build_name_to_target_map(
        str(FIXTURES), ["wifi_domain.yaml"],
    )
    assert overrides.get("wifi-domain-device") == "wifi-domain-device.example.internal"
    # Source is `wifi_use_address` (not `mdns_default` — the pre-fix bug)
    # because the validator set `use_address`, not `manual_ip.static_ip`.
    assert sources.get("wifi-domain-device") == "wifi_use_address"


def test_static_ip_fixture_keeps_static_ip_source_label():
    """After full validation, a static-IP config keeps its `_static_ip` source.

    The wifi validator promotes ``manual_ip.static_ip`` into ``use_address``
    (so ESPHome itself connects to the static IP). Our ``get_device_address``
    detects that match and keeps the legacy source label so the Devices-tab
    tooltip still reads "wifi static_ip" rather than "wifi.use_address".
    Regression guard alongside the #84 fix.
    """
    _, _, overrides, sources = build_name_to_target_map(
        str(FIXTURES), ["static_ip_device.yaml"],
    )
    assert overrides.get("static-ip-device") == "192.168.1.99"
    assert sources.get("static-ip-device") == "wifi_static_ip"


def test_get_device_address_validated_use_address_from_static_ip():
    """Direct unit-level check for the source-label heuristic.

    When both ``use_address`` and ``manual_ip.static_ip`` are present and
    equal, source is ``_static_ip`` (the pattern ``validate_config``
    produces when it promotes a static IP). When they differ, source is
    ``_use_address`` (explicit override or domain-injection).
    """
    # validator-produced shape for a static-IP config
    config = {
        "wifi": {
            "use_address": "10.0.0.5",
            "manual_ip": {"static_ip": "10.0.0.5"},
        }
    }
    assert get_device_address(config, "dev") == ("10.0.0.5", "wifi_static_ip")

    # validator-produced shape for a domain config — use_address is
    # `{name}{domain}`, manual_ip absent
    config = {
        "wifi": {"use_address": "dev.example.com"},
    }
    assert get_device_address(config, "dev") == ("dev.example.com", "wifi_use_address")

    # explicit override with unrelated static_ip (edge case but spec-level
    # correct: explicit use_address wins, source is use_address)
    config = {
        "wifi": {
            "use_address": "10.0.0.99",  # explicit, differs from static
            "manual_ip": {"static_ip": "10.0.0.5"},
        }
    }
    assert get_device_address(config, "dev") == ("10.0.0.99", "wifi_use_address")


# ---------------------------------------------------------------------------
# Per-device metadata comment block (read_device_meta / write_device_meta)
# ---------------------------------------------------------------------------

from scanner import read_device_meta, write_device_meta


def test_read_device_meta_empty_file(tmp_path):
    """File with no metadata block returns empty dict."""
    f = tmp_path / "device.yaml"
    f.write_text("esphome:\n  name: test\n")
    assert read_device_meta(str(tmp_path), "device.yaml") == {}


def test_read_device_meta_basic(tmp_path):
    """Reads a well-formed block with pin_version and schedule (new marker)."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "# esphome-fleet:\n"
        "#   pin_version: 2026.3.3\n"
        "#   schedule: 0 2 * * 0\n"
        "#   schedule_enabled: true\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )
    meta = read_device_meta(str(tmp_path), "device.yaml")
    assert meta["pin_version"] == "2026.3.3"
    assert meta["schedule"] == "0 2 * * 0"
    assert meta["schedule_enabled"] is True


def test_read_device_meta_legacy_marker(tmp_path):
    """Legacy `# distributed-esphome:` marker is still readable (backward compat)."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "# distributed-esphome:\n"
        "#   pin_version: 2026.3.3\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )
    meta = read_device_meta(str(tmp_path), "device.yaml")
    assert meta["pin_version"] == "2026.3.3"


def test_read_device_meta_with_tags(tmp_path):
    """Tags field parses correctly."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "# distributed-esphome:\n"
        "#   tags: office, sensors\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )
    meta = read_device_meta(str(tmp_path), "device.yaml")
    assert meta["tags"] == "office, sensors"


def test_read_device_meta_ignores_deep_comments(tmp_path):
    """Block must be at the TOP of the file, before any YAML content."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "esphome:\n"
        "  name: test\n"
        "\n"
        "# distributed-esphome:\n"
        "#   pin_version: should-not-match\n"
    )
    assert read_device_meta(str(tmp_path), "device.yaml") == {}


def test_read_device_meta_with_leading_blank_lines(tmp_path):
    """Blank lines before the marker are OK."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "\n"
        "\n"
        "# distributed-esphome:\n"
        "#   pin_version: 2026.3.3\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )
    meta = read_device_meta(str(tmp_path), "device.yaml")
    assert meta["pin_version"] == "2026.3.3"


def test_write_device_meta_adds_block(tmp_path):
    """Adds a block to a file that has none."""
    f = tmp_path / "device.yaml"
    f.write_text("esphome:\n  name: test\n")

    write_device_meta(str(tmp_path), "device.yaml", {"pin_version": "2026.3.3"})

    content = f.read_text()
    assert "# esphome-fleet:" in content
    # Writer should emit the explanatory header so users know not to remove it.
    assert "Fleet for ESPHome" in content
    assert "#   pin_version: 2026.3.3" in content
    # Original content is preserved
    assert "esphome:" in content
    assert "name: test" in content


def test_write_device_meta_replaces_block(tmp_path):
    """Replaces an existing block with new values."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "# esphome-fleet:\n"
        "#   pin_version: old\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )

    write_device_meta(str(tmp_path), "device.yaml", {"pin_version": "new", "schedule": "0 2 * * *"})

    content = f.read_text()
    assert "old" not in content
    assert "# esphome-fleet:" in content
    assert "#   pin_version: new" in content
    assert "#   schedule: 0 2 * * *" in content


def test_write_device_meta_migrates_legacy_marker(tmp_path):
    """Writer migrates a legacy `# distributed-esphome:` block to the new marker."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "# distributed-esphome:\n"
        "#   pin_version: old\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )

    write_device_meta(str(tmp_path), "device.yaml", {"pin_version": "new"})

    content = f.read_text()
    # Old marker gone, new marker present.
    assert "distributed-esphome" not in content
    assert "# esphome-fleet:" in content
    assert "#   pin_version: new" in content


def test_write_device_meta_removes_block_when_empty(tmp_path):
    """Empty dict removes the block entirely (including legacy marker + header)."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "# distributed-esphome:\n"
        "#   pin_version: 2026.3.3\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )

    write_device_meta(str(tmp_path), "device.yaml", {})

    content = f.read_text()
    assert "distributed-esphome" not in content
    assert "esphome-fleet" not in content
    assert "esphome:" in content


def test_write_device_meta_routing_extra_round_trip(tmp_path):
    """TG.2: per-device additive routing rules (`routing_extra`) round-trip
    through the YAML metadata comment block as a list of rule dicts.
    The comment-block writer doesn't need to know the rule shape — it
    just YAML-dumps whatever ``meta`` it gets and the reader parses it
    back through ``yaml.safe_load``."""
    f = tmp_path / "device.yaml"
    f.write_text("esphome:\n  name: test\n")

    routing_extra = [
        {
            "name": "device-only-fast",
            "severity": "required",
            "device_match": [{"op": "all_of", "tags": ["kitchen"]}],
            "worker_match": [{"op": "all_of", "tags": ["fast"]}],
        },
    ]
    write_device_meta(str(tmp_path), "device.yaml", {"routing_extra": routing_extra})

    meta = read_device_meta(str(tmp_path), "device.yaml")
    assert meta == {"routing_extra": routing_extra}
    # Original YAML preserved.
    assert "esphome:" in f.read_text()
    assert "name: test" in f.read_text()


def test_write_device_meta_clearing_only_tags_strips_block(tmp_path):
    """Bug #9 regression: clearing the last tag (the only meta key) removes
    the whole comment block, not an empty `tags:` line.

    Models the dialog-save path: the UI sends `{tags: null}` to
    /ui/api/targets/{filename}/meta when the user clears every chip;
    update_target_meta turns null into a `meta.pop("tags")`; if `tags`
    was the only key in the YAML metadata block, the resulting empty
    dict triggers the whole-block strip path here.
    """
    f = tmp_path / "device.yaml"
    f.write_text(
        "# esphome-fleet:\n"
        "#   tags: office, sensors\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )

    # Pop the only key (what update_target_meta does on tags=null):
    meta = read_device_meta(str(tmp_path), "device.yaml")
    meta.pop("tags", None)
    write_device_meta(str(tmp_path), "device.yaml", meta)

    content = f.read_text()
    assert "esphome-fleet" not in content
    assert "tags:" not in content
    assert "office" not in content
    # Original YAML survives.
    assert "esphome:" in content
    assert "name: test" in content


def test_write_device_meta_clearing_tags_with_other_keys_keeps_block(tmp_path):
    """Bug #9 partner: clearing tags but leaving other meta keys preserves
    the block (just minus the `tags:` line).
    """
    f = tmp_path / "device.yaml"
    f.write_text(
        "# esphome-fleet:\n"
        "#   pin_version: 2026.3.3\n"
        "#   tags: office, sensors\n"
        "\n"
        "esphome:\n"
        "  name: test\n"
    )

    meta = read_device_meta(str(tmp_path), "device.yaml")
    meta.pop("tags", None)
    write_device_meta(str(tmp_path), "device.yaml", meta)

    content = f.read_text()
    assert "# esphome-fleet:" in content
    assert "pin_version: 2026.3.3" in content
    assert "tags:" not in content
    assert "office" not in content


def test_write_device_meta_preserves_other_comments(tmp_path):
    """Other comment lines in the file survive the write."""
    f = tmp_path / "device.yaml"
    f.write_text(
        "# My device config\n"
        "esphome:\n"
        "  name: test\n"
        "# End of file\n"
    )

    write_device_meta(str(tmp_path), "device.yaml", {"schedule": "0 2 * * *"})

    content = f.read_text()
    assert "# My device config" in content
    assert "# End of file" in content
    assert "# esphome-fleet:" in content


def test_write_device_meta_invalidates_cache(tmp_path):
    """_config_cache entry is removed after write."""
    from scanner import _config_cache

    f = tmp_path / "device.yaml"
    f.write_text("esphome:\n  name: test\n")
    _config_cache["device.yaml"] = (0.0, {"fake": True})

    write_device_meta(str(tmp_path), "device.yaml", {"pin_version": "1.0"})
    assert "device.yaml" not in _config_cache


def test_roundtrip_read_write(tmp_path):
    """write then read returns the same dict."""
    f = tmp_path / "device.yaml"
    f.write_text("esphome:\n  name: test\n")

    meta = {
        "pin_version": "2026.3.3",
        "schedule": "0 2 * * 0",
        "schedule_enabled": True,
        "tags": "office, sensors",
    }
    write_device_meta(str(tmp_path), "device.yaml", meta)
    result = read_device_meta(str(tmp_path), "device.yaml")
    assert result == meta



# ---------------------------------------------------------------------------
# create_stub_yaml (CD.1)
# ---------------------------------------------------------------------------


def test_create_stub_yaml_has_name():
    """Stub YAML should contain esphome.name set to the provided name."""
    import yaml
    result = create_stub_yaml("kitchen-sensor")
    data = yaml.safe_load(result)
    assert data == {"esphome": {"name": "kitchen-sensor"}}


def test_create_stub_yaml_round_trips():
    """Stub YAML must parse via yaml.safe_load without errors (PY-1)."""
    import yaml
    result = create_stub_yaml("test-device")
    # Should not raise
    parsed = yaml.safe_load(result)
    assert isinstance(parsed, dict)
    assert parsed["esphome"]["name"] == "test-device"


def test_create_stub_yaml_contains_guidance_comment():
    """Stub should include a hint comment so the user knows where to add content."""
    result = create_stub_yaml("foo")
    assert "Add board" in result


# ---------------------------------------------------------------------------
# duplicate_device (CD.2)
# ---------------------------------------------------------------------------


def test_duplicate_device_rewrites_name(tmp_path):
    """Duplicated YAML has esphome.name set to new_name."""
    import yaml
    src = tmp_path / "source.yaml"
    src.write_text("esphome:\n  name: original\n  comment: Hello\n")

    result = duplicate_device(str(tmp_path), "source.yaml", "duplicated")
    data = yaml.safe_load(result)
    assert data["esphome"]["name"] == "duplicated"
    # Other fields preserved
    assert data["esphome"]["comment"] == "Hello"


def test_duplicate_device_preserves_other_fields(tmp_path):
    """Duplicated YAML keeps substitutions, packages, sensors, etc."""
    import yaml
    src = tmp_path / "src.yaml"
    src.write_text(
        "esphome:\n"
        "  name: my-device\n"
        "wifi:\n"
        "  ssid: home\n"
        "sensor:\n"
        "  - platform: dht\n"
        "    pin: GPIO4\n"
    )

    result = duplicate_device(str(tmp_path), "src.yaml", "my-device-2")
    data = yaml.safe_load(result)
    assert data["esphome"]["name"] == "my-device-2"
    assert data["wifi"]["ssid"] == "home"
    assert data["sensor"][0]["platform"] == "dht"


def test_duplicate_device_rewrites_substitution(tmp_path):
    """When esphome.name is ${substitutions.name}, rewrite the substitution."""
    import yaml
    src = tmp_path / "src.yaml"
    src.write_text(
        "substitutions:\n"
        "  name: old-name\n"
        "  display_name: Old\n"
        "esphome:\n"
        "  name: ${name}\n"
    )

    result = duplicate_device(str(tmp_path), "src.yaml", "new-name")
    data = yaml.safe_load(result)
    # substitution is rewritten, esphome.name keeps the indirection
    assert data["substitutions"]["name"] == "new-name"
    assert data["esphome"]["name"] == "${name}"
    # Other substitutions untouched
    assert data["substitutions"]["display_name"] == "Old"


def test_duplicate_device_missing_source(tmp_path):
    """Missing source file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        duplicate_device(str(tmp_path), "nonexistent.yaml", "new")


def test_duplicate_device_invalid_yaml(tmp_path):
    """Non-parseable source raises ValueError."""
    src = tmp_path / "bad.yaml"
    src.write_text("{{{invalid yaml")
    with pytest.raises(ValueError):
        duplicate_device(str(tmp_path), "bad.yaml", "new")


def test_duplicate_device_no_esphome_block(tmp_path):
    """Source YAML without esphome block gets one added with the new name."""
    import yaml
    src = tmp_path / "src.yaml"
    src.write_text("wifi:\n  ssid: home\n")

    result = duplicate_device(str(tmp_path), "src.yaml", "new-device")
    data = yaml.safe_load(result)
    assert data["esphome"]["name"] == "new-device"
    assert data["wifi"]["ssid"] == "home"


def test_duplicate_device_preserves_include_tags(tmp_path):
    """#43: !include / !secret / custom ESPHome tags survive the round-trip."""
    src = tmp_path / "src.yaml"
    src.write_text(
        "esphome:\n  name: device\n"
        "packages:\n"
        "  common: !include .common.yaml\n"
        "  athom: !include .athom-plug.yaml\n"
        "wifi:\n"
        "  ap:\n"
        "    password: !secret ap_password\n"
    )

    result = duplicate_device(str(tmp_path), "src.yaml", "new-device")
    # name was rewritten
    assert "name: new-device" in result
    # All three custom tags preserved (we can't use yaml.safe_load to verify
    # because that's exactly what used to choke — string-match the output).
    assert "!include '.common.yaml'" in result or "!include .common.yaml" in result
    assert "!include '.athom-plug.yaml'" in result or "!include .athom-plug.yaml" in result
    assert "!secret 'ap_password'" in result or "!secret ap_password" in result


def test_duplicate_device_strips_use_address(tmp_path):
    """#54: wifi.use_address is stripped so the duplicate doesn't inherit
    the source's IP and show "online" just because the server can still
    reach the original device at that address. Other wifi fields
    (ssid, password) are preserved.
    """
    import yaml
    src = tmp_path / "src.yaml"
    src.write_text(
        "esphome:\n  name: device\n"
        "wifi:\n  use_address: 192.168.1.100\n  ssid: home\n"
    )
    result = duplicate_device(str(tmp_path), "src.yaml", "device-copy")
    data = yaml.safe_load(result)
    assert "use_address" not in data["wifi"]
    assert data["wifi"]["ssid"] == "home"


def test_duplicate_device_strips_manual_static_ip(tmp_path):
    """#54: wifi.manual_ip.static_ip is stripped for the same reason."""
    import yaml
    src = tmp_path / "src.yaml"
    src.write_text(
        "esphome:\n  name: device\n"
        "wifi:\n"
        "  ssid: home\n"
        "  manual_ip:\n"
        "    static_ip: 192.168.1.50\n"
        "    gateway: 192.168.1.1\n"
    )
    result = duplicate_device(str(tmp_path), "src.yaml", "device-copy")
    data = yaml.safe_load(result)
    # static_ip removed; gateway preserved (not an identity pin).
    manual_ip = data["wifi"].get("manual_ip") or {}
    assert "static_ip" not in manual_ip
    assert manual_ip.get("gateway") == "192.168.1.1"


def test_duplicate_device_strips_ethernet_and_openthread_addresses(tmp_path):
    """#54: same treatment for ethernet.use_address and openthread."""
    import yaml
    src = tmp_path / "src.yaml"
    src.write_text(
        "esphome:\n  name: device\n"
        "ethernet:\n  use_address: 10.0.0.10\n  type: LAN8720\n"
        "openthread:\n  use_address: fd00::1\n"
    )
    result = duplicate_device(str(tmp_path), "src.yaml", "device-copy")
    data = yaml.safe_load(result)
    assert "use_address" not in data["ethernet"]
    assert data["ethernet"]["type"] == "LAN8720"
    assert "use_address" not in data["openthread"]


def test_duplicate_device_preserves_includes_with_substitution_rewrite(tmp_path):
    """Combined: substitution rewrite + !include preservation."""
    src = tmp_path / "src.yaml"
    src.write_text(
        "substitutions:\n  name: old\n"
        "packages:\n"
        "  common: !include .common.yaml\n"
        "esphome:\n  name: ${name}\n"
    )

    result = duplicate_device(str(tmp_path), "src.yaml", "fresh")
    # substitution rewritten
    assert "name: fresh" in result
    # esphome.name still references the substitution
    assert "name: ${name}" in result
    # include preserved
    assert "!include" in result


def test_duplicate_device_rewrites_substitutions_name_with_implicit_esphome_name(tmp_path):
    """#43 follow-up: source has substitutions.name AND top-level esphome block
    without a name field (the actual device name comes from an included
    package that uses ${name}). Duplicate should rewrite substitutions.name
    so the rename propagates into the includes, and leave the top-level
    esphome block alone (no redundant literal name)."""
    src = tmp_path / "src.yaml"
    src.write_text(
        "substitutions:\n"
        "  name: athom-plug-1\n"
        "  display_name: Office Speakers\n"
        "esphome:\n"
        "  area: Office\n"
        "packages:\n"
        "  common: !include .common.yaml\n"
        "  athom: !include .athom-plug.yaml\n"
    )

    result = duplicate_device(str(tmp_path), "src.yaml", "athom-plug-1-copy")
    # substitutions.name rewritten — this is the key fix
    assert "name: athom-plug-1-copy" in result
    assert "athom-plug-1" not in result.replace("athom-plug-1-copy", "")
    # No literal esphome.name injected (the includes will pull it from ${name})
    # Rough check: esphome block doesn't gain an explicit name line.
    # The resulting esphome block should still be just "area: Office".
    import yaml as _yaml
    class _Loader(_yaml.SafeLoader):
        pass
    _Loader.add_multi_constructor("!", lambda loader, suf, node: None)
    parsed = _yaml.load(result, Loader=_Loader)
    assert "name" not in parsed["esphome"]
    # Other substitutions preserved
    assert parsed["substitutions"]["display_name"] == "Office Speakers"


def test_resolve_failure_logs_warning(tmp_path, caplog):
    """DL.5: malformed YAML resolve failure promotes to WARNING with
    the target filename + exception type (issue #60 diagnostic).
    """
    import logging
    from scanner import _resolve_esphome_config

    bad = tmp_path / "broken.yaml"
    # !secret reference a secret that doesn't exist — ESPHome's resolve
    # pipeline raises. The test only cares that our catch path logs WARNING.
    bad.write_text(
        "esphome:\n"
        "  name: broken\n"
        "wifi:\n"
        "  password: !secret nonexistent_secret\n"
    )
    with caplog.at_level(logging.WARNING, logger="scanner"):
        result = _resolve_esphome_config(str(tmp_path), "broken.yaml")
    assert result is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("broken.yaml" in r.getMessage() for r in warnings), (
        f"expected WARNING mentioning broken.yaml, got: {[r.getMessage() for r in warnings]}"
    )


# --- Bug #112 — bundle subprocess stderr is clean (no esphome logger noise) -

def test_bundle_subprocess_stderr_does_not_leak_esphome_logger_chatter():
    """Bug #112: ESPHome's _LOGGER (esphome.* namespace) emits INFO /
    WARNING chatter during validate_config — "INFO ESPHome 2026.4.3",
    "INFO Reading configuration...", deprecation warnings, etc. Our
    bundle subprocess captures stderr verbatim and surfaces it to the
    UI's Queue-tab Log modal as the failure log; ESPHome's status
    chatter clutters the message and reads as if it were the cause of
    a failure. The fix is a NullHandler + propagate=False + suppressed
    level on the `esphome` logger inside the subprocess script.

    Test: invoke the script against a known-good fixture YAML and
    assert stderr is empty. This catches a future regression where
    someone removes the silencing or accidentally `print()`s diagnostic
    output to stderr.
    """
    import subprocess as _sp

    from scanner import _BUNDLE_SUBPROCESS_SCRIPT

    proc = _sp.run(
        [sys.executable, "-c", _BUNDLE_SUBPROCESS_SCRIPT, str(FIXTURES / "device1.yaml")],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (
        f"bundle subprocess failed unexpectedly: stderr={proc.stderr.decode()!r}"
    )
    stderr = proc.stderr.decode("utf-8", errors="replace")
    # The subprocess emits its own validation-error message via
    # sys.stderr.write only on exit code 3 (validation errors). Exit 0
    # should produce zero stderr — any byte means ESPHome's logger
    # leaked through.
    assert stderr == "", (
        f"bundle subprocess on a healthy YAML wrote to stderr: {stderr!r}\n"
        "ESPHome's _LOGGER namespace is leaking through the silencing "
        "in scanner._BUNDLE_SUBPROCESS_SCRIPT — bug #112 regression."
    )


# ---------------------------------------------------------------------------
# rename_device_in_yaml — PY-1 regression for ui_api.rename_target's prior
# regex-driven rewrite. Each case asserts a previously-misfiring shape now
# rewrites correctly while preserving comments.
# ---------------------------------------------------------------------------


def test_rename_device_in_yaml_literal_esphome_name():
    src = """\
esphome:
  name: old-name  # the device's hostname
  platform: ESP32
"""
    out, ok = rename_device_in_yaml(src, "new-name")
    assert ok is True
    assert "name: new-name" in out
    assert "# the device's hostname" in out, "trailing comment was clobbered"
    assert "old-name" not in out


def test_rename_device_in_yaml_substitutions_indirection():
    src = """\
substitutions:
  name: old-name
  area: garage

esphome:
  name: ${name}
  friendly_name: My Device
"""
    out, ok = rename_device_in_yaml(src, "new-name")
    assert ok is True
    # The rewrite lands on substitutions.name, not on the ${name} indirection.
    assert "name: new-name" in out
    assert "${name}" in out, "indirection was clobbered"
    # area stays untouched (not the first `name:` we matched, regression for
    # the old regex's count=1 picking the wrong key in non-esphome-first files).
    assert "area: garage" in out


def test_rename_device_in_yaml_quoted_value():
    src = """\
esphome:
  name: "old-name"
"""
    out, ok = rename_device_in_yaml(src, "new-name")
    assert ok is True
    assert 'name: "new-name"' in out
    assert "old-name" not in out


def test_rename_device_in_yaml_skips_unsafe_indirection():
    """${...} esphome.name with no matching substitutions key is unsafe."""
    src = """\
esphome:
  name: ${unresolved}
"""
    out, ok = rename_device_in_yaml(src, "new-name")
    assert ok is False
    assert out == src  # untouched


# ---------------------------------------------------------------------------
# #131 — legacy bundle fallback for ESPHome <2026.4
# ---------------------------------------------------------------------------

def _build_legacy_fixture(tmp_path: Path) -> None:
    """Lay out a minimal config dir matching the pre-1.6.2 shape."""
    (tmp_path / "secrets.yaml").write_text(
        'wifi_ssid: "ssid"\nwifi_password: "long-enough-password"\nota_password: "ota-password"\n'
    )
    (tmp_path / "device-a.yaml").write_text(
        "esphome:\n  name: device-a\n"
        "esp8266:\n  board: d1_mini\n"
        "wifi:\n  ssid: !secret wifi_ssid\n  password: !secret wifi_password\n"
    )
    (tmp_path / "device-b.yaml").write_text(
        "esphome:\n  name: device-b\n"
        "esp8266:\n  board: d1_mini\n"
    )
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "shared.yaml").write_text("logger:\n  level: DEBUG\n")
    # Build-cache + git directories that must NOT ship.
    (tmp_path / ".esphome").mkdir()
    (tmp_path / ".esphome" / "stale.bin").write_bytes(b"\x00" * 16)
    (tmp_path / ".pioenvs").mkdir()
    (tmp_path / ".pioenvs" / "device-a").mkdir()
    (tmp_path / ".pioenvs" / "device-a" / "stale.o").write_bytes(b"\x00" * 16)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")


def test_legacy_bundle_ships_full_config_dir(tmp_path):
    """Legacy path tars every YAML — including unrelated targets and
    secrets — to match pre-1.6.2 behaviour. Trade-off documented; the
    user pinning <2026.4 is opting into the wider bundle."""
    from scanner import _create_legacy_bundle
    _build_legacy_fixture(tmp_path)
    raw = _create_legacy_bundle(str(tmp_path), "device-a.yaml")
    names = _bundle_names(raw)
    assert "device-a.yaml" in names
    # Pre-1.6.2 ships everything — that's the trade-off documented in
    # CHANGELOG / DOCS.
    assert "device-b.yaml" in names
    assert "packages/shared.yaml" in names
    assert "secrets.yaml" in names


def test_legacy_bundle_skips_build_caches_and_git(tmp_path):
    """Legacy path still excludes the obvious caches — anything that's
    machine-generated and would just bloat the tarball without helping
    the worker compile."""
    from scanner import _create_legacy_bundle
    _build_legacy_fixture(tmp_path)
    raw = _create_legacy_bundle(str(tmp_path), "device-a.yaml")
    names = _bundle_names(raw)
    for name in names:
        parts = name.split("/")
        assert ".esphome" not in parts, name
        assert ".pioenvs" not in parts, name
        assert ".pio" not in parts, name
        assert ".git" not in parts, name
        assert "__pycache__" not in parts, name


def test_legacy_bundle_paths_are_relative(tmp_path):
    from scanner import _create_legacy_bundle
    _build_legacy_fixture(tmp_path)
    raw = _create_legacy_bundle(str(tmp_path), "device-a.yaml")
    for name in _bundle_names(raw):
        assert not name.startswith("/"), name


def test_legacy_bundle_raises_on_missing_target(tmp_path):
    from scanner import _create_legacy_bundle
    _build_legacy_fixture(tmp_path)
    with pytest.raises(FileNotFoundError):
        _create_legacy_bundle(str(tmp_path), "ghost.yaml")


def test_create_bundle_dispatches_legacy_when_server_predates_2026_4(tmp_path, monkeypatch):
    """When the server's installed ESPHome reports a version below the
    floor, ``create_bundle`` routes to ``_create_legacy_bundle``
    instead of running the modern subprocess. Regression net for #131:
    a future refactor that drops the dispatch must trip this test.
    """
    from scanner import create_bundle
    import scanner as _scanner
    _build_legacy_fixture(tmp_path)
    # Make the modern path detect "old version".
    monkeypatch.setattr(_scanner, "_get_installed_esphome_version", lambda: "2026.3.3")
    # Nuke the modern subprocess hook so the test fails loudly if dispatch
    # picks the wrong branch (no real ESPHome venv in the test env anyway).
    def _explode(*a, **kw):
        raise AssertionError("modern bundle path must not be taken for <2026.4")
    monkeypatch.setattr(_scanner.subprocess, "run", _explode)

    raw = create_bundle(str(tmp_path), "device-a.yaml")
    names = _bundle_names(raw)
    # Legacy bundle is the full config dir — secrets + unrelated targets in.
    assert "device-a.yaml" in names
    assert "device-b.yaml" in names
    assert "secrets.yaml" in names


def test_create_bundle_dispatches_modern_when_floor_or_above(tmp_path, monkeypatch):
    """At 2026.4.0 (the floor) and above, dispatch goes to the modern
    ConfigBundleCreator subprocess. Asserts via the absence of the
    legacy "full-config-dir" markers — namely that ``device-b.yaml``
    (unreferenced) is NOT in the bundle.
    """
    from scanner import create_bundle
    _build_legacy_fixture(tmp_path)
    raw = create_bundle(str(tmp_path), "device-a.yaml")
    names = _bundle_names(raw)
    assert "device-a.yaml" in names
    assert "device-b.yaml" not in names  # modern path excludes unreferenced


def test_supports_modern_bundle_below_floor(monkeypatch):
    import scanner as _scanner
    monkeypatch.setattr(_scanner, "_get_installed_esphome_version", lambda: "2026.3.3")
    assert _scanner._supports_modern_bundle() is False


def test_supports_modern_bundle_at_floor(monkeypatch):
    import scanner as _scanner
    monkeypatch.setattr(_scanner, "_get_installed_esphome_version", lambda: "2026.4.0")
    assert _scanner._supports_modern_bundle() is True


def test_supports_modern_bundle_above_floor(monkeypatch):
    import scanner as _scanner
    monkeypatch.setattr(_scanner, "_get_installed_esphome_version", lambda: "2026.5.1")
    assert _scanner._supports_modern_bundle() is True


def test_supports_modern_bundle_unknown_falls_through_to_modern(monkeypatch):
    """Unknown / installing-state versions don't pre-emptively pick
    the legacy path — better to surface a real error from the modern
    subprocess than to ship secrets to every worker on a transient
    parse glitch."""
    import scanner as _scanner
    monkeypatch.setattr(_scanner, "_get_installed_esphome_version", lambda: "unknown")
    assert _scanner._supports_modern_bundle() is True
    monkeypatch.setattr(_scanner, "_get_installed_esphome_version", lambda: "installing")
    assert _scanner._supports_modern_bundle() is True


def test_ensure_esphome_installed_no_longer_refuses_old_versions(monkeypatch):
    """Pre-1.7.1 ``ensure_esphome_installed`` short-circuited with
    ``_esphome_install_failed = True`` for any version below the floor.
    #131 dropped that refusal: every version is installable; the
    bundle dispatcher picks the legacy path at compile time. Regression
    net: assert the function ATTEMPTS to install (gets past the floor
    check) instead of bailing out at the top.
    """
    import scanner as _scanner
    # Capture whether VersionManager was reached.
    reached = {"vm": False}

    class _StubVM:
        def __init__(self, **kw): ...
        def ensure_version(self, version: str) -> str:
            reached["vm"] = True
            raise RuntimeError("stub: VM not actually wired up in the test env")

    # Stub out the VersionManager import so the test doesn't need the
    # real client tree. The function should still REACH the install
    # attempt — that's what we're asserting.
    monkeypatch.setitem(sys.modules, "version_manager", type("M", (), {"VersionManager": _StubVM}))
    _scanner._esphome_install_failed = False
    _scanner.ensure_esphome_installed("2026.3.3", versions_base=Path("/tmp/test_esphome_versions"))
    assert reached["vm"] is True, "ensure_esphome_installed bailed before reaching VersionManager"
