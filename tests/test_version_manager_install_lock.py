"""#317 — concurrent installs of one ESPHome version must not clobber each other.

Live repro hass-4 2026-11-03: the user switched to 2026.10.2 in the UI and,
8 s later, the HA ESPHome add-on poller saw the same bump. Each path built
its own ``VersionManager`` on the shared ``/data/esphome-versions``, so the
in-instance ``_installing`` dedupe never saw the other. The second install's
"remove stale venv" wiped the first one mid-pip (``certifi/cacert.pem``
missing), and the in-container local worker (a separate process with its
own manager) later raced the UI retry the same way (``ensurepip`` failing).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from version_manager import VersionManager


def _fake_install(state: dict, lock: threading.Lock):
    """Return an ``_install`` stand-in that records overlap and builds a venv."""

    def _install(self: VersionManager, version: str) -> None:
        with lock:
            state["active"] += 1
            state["calls"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        try:
            time.sleep(0.3)
            venv = self._venv_path(version)
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "esphome").write_text("#!/bin/sh\n")
            (venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}").mkdir(
                parents=True, exist_ok=True,
            )
        finally:
            with lock:
                state["active"] -= 1

    return _install


def test_separate_managers_serialise_and_share_one_install(tmp_path: Path, monkeypatch):
    state = {"active": 0, "max_active": 0, "calls": 0}
    monkeypatch.setattr(VersionManager, "_install", _fake_install(state, threading.Lock()))

    results: list[str] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            # A fresh manager per caller, exactly like scanner.ensure_esphome_installed.
            vm = VersionManager(versions_base=tmp_path, max_versions=5)
            results.append(vm.ensure_version("2026.10.2"))
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert state["max_active"] == 1, "installs of the same version overlapped"
    assert state["calls"] == 1, "later callers should reuse the finished install"
    assert len(results) == 3 and len(set(results)) == 1
