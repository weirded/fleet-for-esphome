"""#171 follow-up — a content-only YAML edit must reseed the device poller.

The config scanner compared target *names* only, so editing an existing
device's ``manual_ip`` never reached ``DevicePoller.update_compile_targets``:
the Devices tab and the OTA override stayed on the old address until a
restart. Found in the 1.8.0 release-PR review.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import main


def _app(config_dir: Path, poller: MagicMock) -> dict:
    cfg = MagicMock()
    cfg.config_dir = str(config_dir)
    return {"config": cfg, "device_poller": poller}


async def test_content_edit_reseeds_poller_without_target_change(tmp_path: Path):
    target = tmp_path / "dev.yaml"
    target.write_text("wifi:\n  manual_ip:\n    static_ip: 10.0.0.1\n")
    poller = MagicMock()
    builds: list[str] = []

    def scan(_d):
        return ["dev.yaml"]

    def build(_d, _targets):
        builds.append(target.read_text())
        return {}, {}, {}, {}

    app = _app(tmp_path, poller)
    state: dict = {}
    await main._config_scan_once(app, state, scan, build)
    assert poller.update_compile_targets.call_count == 1

    # Unchanged tree → no re-validation (idle-efficiency).
    await main._config_scan_once(app, state, scan, build)
    assert poller.update_compile_targets.call_count == 1

    # Same target list, edited content → reseed.
    target.write_text("wifi:\n  manual_ip:\n    static_ip: 10.0.0.2\n")
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    await main._config_scan_once(app, state, scan, build)
    assert poller.update_compile_targets.call_count == 2
    assert "10.0.0.2" in builds[-1]


async def test_included_file_edit_also_reseeds(tmp_path: Path):
    (tmp_path / "dev.yaml").write_text("packages:\n  common: !include common.yaml\n")
    inc = tmp_path / "common.yaml"
    inc.write_text("a: 1\n")
    poller = MagicMock()
    app = _app(tmp_path, poller)
    state: dict = {}

    def scan(_d):
        return ["dev.yaml"]

    def build(_d, _t):
        return {}, {}, {}, {}

    await main._config_scan_once(app, state, scan, build)
    inc.write_text("a: 22\n")
    await main._config_scan_once(app, state, scan, build)
    assert poller.update_compile_targets.call_count == 2


def test_signature_ignores_build_and_archive_dirs(tmp_path: Path):
    (tmp_path / "dev.yaml").write_text("x: 1\n")
    before = main._config_signature(str(tmp_path))
    for d in (".esphome", ".archive"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "junk.yaml").write_text("y: 1\n")
    assert main._config_signature(str(tmp_path)) == before
