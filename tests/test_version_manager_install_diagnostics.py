"""#193 + #240 — the two things a failed ESPHome install has to get right.

#193 — the per-attempt install timeout must be raisable. 300 s is sized for
"slow ARM host", but a Zimaboard reporter measured ~14 minutes for a full
install, so every attempt hit the wall and the version could never finish
installing. There is no UI for it, so it has to be an env var.

#240 — when pip *does* fail, the message has to name the cause. The reported
symptom was a 200-version wall ending in "No matching distribution found for
esphome==2026.7.3", which reads as "that release doesn't exist". It did; pip
had filtered every 2026.7.x candidate out because ESPHome's Python floor had
moved above the shipped image's interpreter.
"""

from __future__ import annotations

import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# #193 — ESPHOME_INSTALL_TIMEOUT
# ---------------------------------------------------------------------------

def _reload_with_env(monkeypatch, value: str | None):
    """Re-import version_manager with ESPHOME_INSTALL_TIMEOUT set to *value*.

    The timeout is resolved at import time (env is fixed for a container's
    lifetime), so the reload is the honest way to exercise it.
    """
    if value is None:
        monkeypatch.delenv("ESPHOME_INSTALL_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("ESPHOME_INSTALL_TIMEOUT", value)
    import version_manager

    return importlib.reload(version_manager)


@pytest.fixture(autouse=True)
def _restore_module(monkeypatch):
    """Leave the module in its default-env state for every other test file."""
    yield
    monkeypatch.delenv("ESPHOME_INSTALL_TIMEOUT", raising=False)
    import version_manager

    importlib.reload(version_manager)


def test_default_install_timeout_is_unchanged(monkeypatch):
    """Unset env keeps the historical 300 s — this fix adds a knob, it does
    not move the default for everyone."""
    vm = _reload_with_env(monkeypatch, None)
    assert vm.ESPHOME_INSTALL_TIMEOUT == 300
    assert vm.VersionManager._PIP_INSTALL_TIMEOUT == 300


def test_env_raises_the_install_timeout(monkeypatch):
    """#193's actual ask: 14 minutes has to be expressible."""
    vm = _reload_with_env(monkeypatch, "900")
    assert vm.ESPHOME_INSTALL_TIMEOUT == 900
    assert vm.VersionManager._PIP_INSTALL_TIMEOUT == 900


def test_install_timeout_is_floored(monkeypatch):
    """A too-small value fails in a way that looks like a network problem;
    clamp rather than let the worker misdiagnose itself."""
    vm = _reload_with_env(monkeypatch, "5")
    assert vm.ESPHOME_INSTALL_TIMEOUT == 60


def test_garbage_install_timeout_falls_back(monkeypatch):
    """This is read at import, before any worker error handling exists — a
    typo in a docker-compose file must not crash the worker at startup."""
    vm = _reload_with_env(monkeypatch, "ten minutes")
    assert vm.ESPHOME_INSTALL_TIMEOUT == 300


def test_blank_install_timeout_falls_back(monkeypatch):
    """`ESPHOME_INSTALL_TIMEOUT=` in an env file is 'unset', not zero."""
    vm = _reload_with_env(monkeypatch, "   ")
    assert vm.ESPHOME_INSTALL_TIMEOUT == 300


# ---------------------------------------------------------------------------
# #240 — diagnose_pip_failure
# ---------------------------------------------------------------------------

# Trimmed from the log pasted in #240.
_PY_FLOOR_STDERR = (
    "ERROR: Ignored the following versions that require a different python "
    "version: 2026.7.0 Requires-Python <3.15,>=3.12.0\n"
    "ERROR: Could not find a version that satisfies the requirement "
    "esphome==2026.7.3 (from versions: 2025.2.0, 2026.6.5)\n"
    "ERROR: No matching distribution found for esphome==2026.7.3\n"
)


def _diagnose(*args):
    from version_manager import diagnose_pip_failure

    return diagnose_pip_failure(*args)


def test_python_floor_failure_is_named_as_such():
    msg = _diagnose("2026.7.3", "", _PY_FLOOR_STDERR)
    assert msg is not None
    lowered = msg.lower()
    # The three things the reporter of #240 had to work out by hand.
    assert "python" in lowered
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in msg
    assert "image" in lowered
    assert "2026.7.3" in msg


def test_python_floor_message_contradicts_the_misleading_pip_wording():
    """pip says the release doesn't exist. It does. Say so explicitly, or the
    next reporter goes looking for a typo in their version pin."""
    msg = _diagnose("2026.7.3", "", _PY_FLOOR_STDERR)
    assert "does exist" in msg.lower()


def test_missing_version_without_python_floor_gets_the_generic_hint():
    """A genuinely bogus pin still gets help, but is not blamed on Python."""
    stderr = "ERROR: No matching distribution found for esphome==9999.1.1\n"
    msg = _diagnose("9999.1.1", "", stderr)
    assert msg is not None
    assert "not found on the package index" in msg


def test_unrelated_failure_returns_none():
    """Wrong guesses are worse than no guess — a build failure keeps the
    existing generic error with pip's own stderr attached."""
    stderr = (
        "error: subprocess-exited-with-error\n"
        "  gcc: fatal error: Killed signal terminated program cc1plus\n"
    )
    assert _diagnose("2026.6.5", "", stderr) is None


def test_diagnosis_reads_stdout_too():
    """pip splits these lines across streams depending on version and TTY."""
    msg = _diagnose("2026.7.3", _PY_FLOOR_STDERR, "")
    assert msg is not None
    assert "python" in msg.lower()


def test_diagnosis_is_case_insensitive():
    """Don't let a pip wording/case change silently disable the diagnosis."""
    msg = _diagnose("2026.7.3", "", _PY_FLOOR_STDERR.upper())
    assert msg is not None
    assert "python" in msg.lower()


# ---------------------------------------------------------------------------
# #243 — a venv built by a different Python must not count as installed
# ---------------------------------------------------------------------------
#
# `_is_installed` used to check only that `bin/esphome` existed. A venv built
# by another interpreter therefore reported as installed, no reinstall was
# attempted, and `scanner._activate_esphome_venv` then failed to find
# `lib/python{running}/site-packages` — surfacing as "ESPHome X install
# failed" with no path to recovery. Live repro: a stale 1.8.0-dev.2 image
# (Python 3.11) deployed on top of a `/data` that a correct 3.13 image had
# already populated.

def _make_venv(tmp_path, version: str, pythons: list[str]):
    """Build a fake version venv containing `lib/python*/` for each entry."""
    from version_manager import VersionManager

    base = tmp_path / "versions"
    vm = VersionManager.__new__(VersionManager)
    vm._base = base  # type: ignore[attr-defined]
    venv = base / version
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "esphome").write_text("#!/bin/sh\n")
    for py in pythons:
        (venv / "lib" / py / "site-packages").mkdir(parents=True)
    return vm


def test_venv_from_this_interpreter_counts_as_installed(tmp_path):
    running = f"python{sys.version_info.major}.{sys.version_info.minor}"
    vm = _make_venv(tmp_path, "2026.8.1", [running])
    assert vm._is_installed("2026.8.1") is True


def test_venv_from_another_interpreter_is_not_installed(tmp_path):
    """The #243 case: 3.13-built venv, 3.11 image. Must rebuild, not claim OK."""
    vm = _make_venv(tmp_path, "2026.8.1", ["python3.11", "python3.13"])
    running = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if running in ("python3.11", "python3.13"):
        pytest.skip("running interpreter collides with the fixture versions")
    assert vm._is_installed("2026.8.1") is False


def test_patch_level_bump_does_not_force_a_rebuild(tmp_path):
    """3.13.1 -> 3.13.2 keeps the same lib/python3.13/, so the venv is fine.
    Checking pyvenv.cfg's full version string instead would churn a rebuild
    on every base-image patch bump."""
    running = f"python{sys.version_info.major}.{sys.version_info.minor}"
    vm = _make_venv(tmp_path, "2026.8.1", [running])
    (tmp_path / "versions" / "2026.8.1" / "pyvenv.cfg").write_text(
        "version = 9.9.9\nhome = /usr/local/bin\n"
    )
    assert vm._is_installed("2026.8.1") is True


def test_missing_binary_is_still_not_installed(tmp_path):
    running = f"python{sys.version_info.major}.{sys.version_info.minor}"
    vm = _make_venv(tmp_path, "2026.8.1", [running])
    (tmp_path / "versions" / "2026.8.1" / "bin" / "esphome").unlink()
    assert vm._is_installed("2026.8.1") is False


def test_unreadable_lib_dir_is_treated_as_not_installed(tmp_path):
    """A venv we can't verify is one to rebuild, not one to trust."""
    from version_manager import VersionManager

    base = tmp_path / "versions"
    vm = VersionManager.__new__(VersionManager)
    vm._base = base  # type: ignore[attr-defined]
    venv = base / "2026.8.1"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "esphome").write_text("#!/bin/sh\n")
    # No lib/ at all — a half-built venv.
    assert vm._is_installed("2026.8.1") is False
