"""#154 (GitHub): git-URL ``packages:`` lose their ``!secret`` keys in the bundle.

ESPHome 2026.4–2026.7's ``ConfigBundleCreator`` only scans *local* YAML for
``!secret`` references, so a target whose secrets are all used inside a
git-fetched package ships with no ``secrets.yaml`` at all and every worker
fails with "Error reading file .../secrets.yaml". Fixed upstream in 2026.8.0
(esphome/esphome#18053); ``scanner.merge_remote_package_secrets`` backfills
the same keys when the server bundles with an older ESPHome.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import yaml

import scanner


def _make_bundle(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _read_bundle(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            f = tar.extractfile(m)
            assert f is not None
            out[m.name] = f.read()
    return out


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "esphome"
    config_dir.mkdir()
    (config_dir / "secrets.yaml").write_text(
        "wifi_ssid: testssid\nwifi_password: testpass123\n"
        "local_key: localval\nother_device_key: nope\n"
    )
    repo = config_dir / ".esphome" / "packages" / "35a3f5d3"
    repo.mkdir(parents=True)
    (repo / "generic-wifi.yaml").write_text(
        "wifi:\n  ssid: !secret wifi_ssid\n  password: !secret 'wifi_password'\n"
    )
    (repo / ".git").mkdir()
    (repo / ".git" / "junk.yaml").write_text("x: !secret other_device_key\n")
    return config_dir, repo


def _manifest(has_secrets: bool) -> bytes:
    return json.dumps({"manifest_version": 1, "has_secrets": has_secrets}).encode()


def test_missing_secrets_file_is_added_from_remote_package(tmp_path: Path) -> None:
    """Exact shape of the reported failure: bundle has no secrets.yaml."""
    config_dir, repo = _setup(tmp_path)
    bundle = _make_bundle({
        "manifest.json": _manifest(False),
        "analog-co2.yaml": b"packages: {}\n",
    })

    out = _read_bundle(scanner.merge_remote_package_secrets(bundle, config_dir, [repo]))

    assert yaml.safe_load(out["secrets.yaml"]) == {
        "wifi_ssid": "testssid", "wifi_password": "testpass123",
    }
    assert json.loads(out["manifest.json"])["has_secrets"] is True
    assert out["analog-co2.yaml"] == b"packages: {}\n"


def test_existing_filtered_secrets_are_kept_and_extended(tmp_path: Path) -> None:
    config_dir, repo = _setup(tmp_path)
    bundle = _make_bundle({
        "manifest.json": _manifest(True),
        "secrets.yaml": b"local_key: localval\n",
        "dev.yaml": b"x: 1\n",
    })

    out = _read_bundle(scanner.merge_remote_package_secrets(bundle, config_dir, [repo]))

    assert yaml.safe_load(out["secrets.yaml"]) == {
        "local_key": "localval", "wifi_ssid": "testssid", "wifi_password": "testpass123",
    }
    # Keys only referenced under .git/ (or by unrelated devices) never ship.
    assert b"other_device_key" not in out["secrets.yaml"]


def test_no_package_dirs_returns_bundle_unchanged(tmp_path: Path) -> None:
    config_dir, _ = _setup(tmp_path)
    bundle = _make_bundle({"manifest.json": _manifest(False), "d.yaml": b"x: 1\n"})
    assert scanner.merge_remote_package_secrets(bundle, config_dir, []) is bundle


def test_nothing_missing_returns_bundle_unchanged(tmp_path: Path) -> None:
    """2026.8+-style bundle that already carries the keys is left byte-identical."""
    config_dir, repo = _setup(tmp_path)
    bundle = _make_bundle({
        "manifest.json": _manifest(True),
        "secrets.yaml": b"wifi_ssid: testssid\nwifi_password: testpass123\n",
    })
    assert scanner.merge_remote_package_secrets(bundle, config_dir, [repo]) is bundle


def test_no_config_dir_secrets_returns_bundle_unchanged(tmp_path: Path) -> None:
    config_dir, repo = _setup(tmp_path)
    (config_dir / "secrets.yaml").unlink()
    bundle = _make_bundle({"manifest.json": _manifest(False)})
    assert scanner.merge_remote_package_secrets(bundle, config_dir, [repo]) is bundle


def test_subprocess_marker_lines_are_parsed() -> None:
    stderr = (
        b"some noise\n"
        + scanner.REMOTE_PKG_DIR_MARKER.encode() + b"/cfg/.esphome/packages/abc\n"
        + scanner.REMOTE_PKG_DIR_MARKER.encode() + b"/cfg/.esphome/packages/abc\n"
        + scanner.REMOTE_PKG_DIR_MARKER.encode() + b"/cfg/.esphome/packages/def\n"
    )
    assert scanner._parse_remote_pkg_dirs(stderr) == [
        Path("/cfg/.esphome/packages/abc"), Path("/cfg/.esphome/packages/def"),
    ]
