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
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_defaults(monkeypatch):
    _set(monkeypatch)
    cfg = config_mod.from_env()
    cfg.validate()
    assert cfg.workers_per_node == 1
    assert cfg.per_node_overrides == {}
    assert cfg.min_total_workers == 0
    assert cfg.max_total_workers is None
    assert cfg.worker_tag == "esphome-fleet-worker"
    assert cfg.target_per_worker == 2


def test_strips_trailing_slash_on_fleet_url(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_FLEET_URL="http://example/")
    assert config_mod.from_env().fleet_url == "http://example"


def test_per_node_overrides_parsed(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_PER_NODE_OVERRIDES="pve-beast:4,pve-tiny:0,pve3:2")
    cfg = config_mod.from_env()
    assert cfg.per_node_overrides == {"pve-beast": 4, "pve-tiny": 0, "pve3": 2}
    assert cfg.per_node_max("pve-beast") == 4
    assert cfg.per_node_max("pve-tiny") == 0
    # Nodes not in overrides fall back to the default.
    assert cfg.per_node_max("pve-default-node") == cfg.workers_per_node


def test_per_node_overrides_whitespace_tolerant(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_PER_NODE_OVERRIDES=" pve1 : 2 , pve2 : 3 ")
    cfg = config_mod.from_env()
    assert cfg.per_node_overrides == {"pve1": 2, "pve2": 3}


def test_per_node_overrides_rejects_garbage(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_PER_NODE_OVERRIDES="pve1:abc")
    with pytest.raises(ValueError, match="count must be int"):
        config_mod.from_env()


def test_per_node_overrides_rejects_missing_count(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_PER_NODE_OVERRIDES="pve1")
    with pytest.raises(ValueError, match="must be 'node:count'"):
        config_mod.from_env()


def test_max_total_optional(monkeypatch):
    _set(monkeypatch)
    assert config_mod.from_env().max_total_workers is None
    _set(monkeypatch, PROXMOX_SCALER_MAX_TOTAL_WORKERS="5")
    assert config_mod.from_env().max_total_workers == 5


def test_validate_missing_token(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_FLEET_TOKEN="")
    cfg = config_mod.from_env()
    with pytest.raises(ValueError, match="FLEET_TOKEN"):
        cfg.validate()


def test_validate_negative_per_node(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_PER_NODE_OVERRIDES="pve-bad:-1")
    cfg = config_mod.from_env()
    with pytest.raises(ValueError, match="must be >= 0"):
        cfg.validate()


def test_validate_max_below_min(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_MIN_TOTAL_WORKERS="5", PROXMOX_SCALER_MAX_TOTAL_WORKERS="2")
    cfg = config_mod.from_env()
    with pytest.raises(ValueError, match="max_total_workers must be"):
        cfg.validate()


def test_worker_tag_override(monkeypatch):
    _set(monkeypatch, PROXMOX_SCALER_WORKER_TAG="my-fleet")
    assert config_mod.from_env().worker_tag == "my-fleet"
