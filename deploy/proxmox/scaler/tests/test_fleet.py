"""Unit tests for FleetClient. Network calls are stubbed via the requests_mock-style adapter."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
import requests

from esphome_fleet_proxmox_scaler.fleet import FleetClient


def _mock_response(status_code: int = 200, body: dict | None = None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = body or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_status_parses_known_fields():
    client = FleetClient("http://example", "tok")
    fake = _mock_response(200, {
        "esphome_version": "2026.4.3",
        "online_workers": 4,
        "online_clients": 4,
        "queue_size": 7,
    })
    with patch.object(client._session, "get", return_value=fake) as get:
        s = client.status()
        get.assert_called_once_with("http://example/api/v1/status", timeout=client.timeout)
    assert s.esphome_version == "2026.4.3"
    assert s.online_workers == 4
    assert s.queue_size == 7


def test_status_defaults_when_fields_missing():
    client = FleetClient("http://example", "tok")
    fake = _mock_response(200, {})
    with patch.object(client._session, "get", return_value=fake):
        s = client.status()
    assert s.esphome_version == ""
    assert s.online_workers == 0
    assert s.queue_size == 0


def test_status_raises_on_http_error():
    client = FleetClient("http://example", "tok")
    fake = _mock_response(401)
    with patch.object(client._session, "get", return_value=fake):
        with pytest.raises(requests.HTTPError):
            client.status()


def test_bearer_header_set():
    client = FleetClient("http://example", "secret-token")
    assert client._session.headers["Authorization"] == "Bearer secret-token"


def test_base_url_trailing_slash_stripped():
    client = FleetClient("http://example/", "tok")
    assert client.base_url == "http://example"
