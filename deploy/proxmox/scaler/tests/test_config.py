"""Tests for env-var → Config parsing + validation."""

from __future__ import annotations

import pytest

from esphome_fleet_proxmox_scaler import config as config_mod


def _set(monkeypatch, **env: str) -> None:
    base = {
        "PROXMOX_SCALER_FLEET_URL": "http://example",
        "PROXMOX_SCALER_FLEET_TOKEN": "tok",
        "PROXMOX_SCALER_PROXMOX_HOST": "pve.example.com:8006",
        "PROXMOX_SCALER_PROXMOX_TOKEN_ID": "root@pam!scaler",
        "PROXMOX_SCALER_PROXMOX_TOKEN_SECRET": "secret",
        "PROXMOX_SCALER_PROXMOX_NODE": "pve",
        "PROXMOX_SCALER_VMIDS": "200,201,202",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_from_env_minimal_valid(monkeypatch):
    _set(monkeypatch)
    cfg = config_mod.from_env()
    cfg.validate()
    assert cfg.vmids == (200, 201, 202)
    assert cfg.min_workers == 0
    assert cfg.max_workers == 3
    assert cfg.target_per_worker == 2


def test_from_env_strips_trailing_slash_on_fleet_url(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_FLEET_URL="http://example/")
    cfg = config_mod.from_env()
    assert cfg.fleet_url == "http://example"


def test_validate_missing_token(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_FLEET_TOKEN="")
    cfg = config_mod.from_env()
    with pytest.raises(ValueError, match="FLEET_TOKEN"):
        cfg.validate()


def test_validate_max_exceeds_pool(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_VMIDS="200,201", PROXMOX_SCALER_MAX_WORKERS="5")
    cfg = config_mod.from_env()
    with pytest.raises(ValueError, match="exceeds pool size"):
        cfg.validate()


def test_validate_max_below_min(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_MIN_WORKERS="3", PROXMOX_SCALER_MAX_WORKERS="2")
    cfg = config_mod.from_env()
    with pytest.raises(ValueError, match="max_workers must be"):
        cfg.validate()


def test_vmids_parser_handles_whitespace(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_VMIDS=" 200 , 201, 202 ")
    cfg = config_mod.from_env()
    assert cfg.vmids == (200, 201, 202)


def test_vmids_parser_rejects_garbage(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_VMIDS="200,abc,202")
    with pytest.raises(ValueError, match="comma-separated list of integers"):
        config_mod.from_env()


def test_bool_env_truthiness(monkeypatch):
    for raw, expected in [("1", True), ("true", True), ("YES", True), ("on", True),
                          ("0", False), ("false", False), ("", True)]:  # default true
        if raw == "":
            monkeypatch.delenv("PROXMOX_SCALER_PROXMOX_VERIFY_SSL", raising=False)
        else:
            monkeypatch.setenv("PROXMOX_SCALER_PROXMOX_VERIFY_SSL", raw)
        _set(monkeypatch)
        if raw != "":
            monkeypatch.setenv("PROXMOX_SCALER_PROXMOX_VERIFY_SSL", raw)
        cfg = config_mod.from_env()
        assert cfg.proxmox_verify_ssl is expected, f"raw={raw!r}"
