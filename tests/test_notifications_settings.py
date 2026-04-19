"""Tests for GUI-managed notification settings."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.auth import _COOKIE_NAME, create_session_cookie
from app.main import app
from app import config_store
from app.services import notification


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "_CONFIG_FILE", tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={_COOKIE_NAME: create_session_cookie()},
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_defaults_when_nothing_saved(client):
    res = await client.get("/api/settings/notifications")
    assert res.status_code == 200
    data = res.json()
    assert data["dsm_notify"] is False
    assert data["pushover_enabled"] is False
    assert data["pushover_api_token_set"] is False
    assert data["pushover_user_key_set"] is False
    assert data["pushover_devices"] == ""


@pytest.mark.asyncio
async def test_save_full_settings(client):
    payload = {
        "dsm_notify": True,
        "pushover_enabled": True,
        "pushover_api_token": "abcdefghij1234567890",
        "pushover_user_key": "uvwxyz1234567890abcd",
        "pushover_devices": "iphone,ipad",
    }
    res = await client.post("/api/settings/notifications", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["dsm_notify"] is True
    assert data["pushover_enabled"] is True
    assert data["pushover_api_token_set"] is True
    assert data["pushover_user_key_set"] is True
    assert data["pushover_devices"] == "iphone,ipad"
    # API must never return the full secret
    assert "abcdefghij" not in data["pushover_api_token_hint"]
    assert data["pushover_api_token_hint"].endswith("7890")

    stored = config_store.get_notifications()
    assert stored["pushover_api_token"] == "abcdefghij1234567890"
    assert stored["pushover_user_key"] == "uvwxyz1234567890abcd"


@pytest.mark.asyncio
async def test_empty_secret_keeps_stored_value(client):
    config_store.save_notifications({
        "dsm_notify": False,
        "pushover_enabled": True,
        "pushover_api_token": "keep-me-token",
        "pushover_user_key": "keep-me-user",
        "pushover_devices": "",
    })
    # Simulate the GUI: user toggles something but leaves the token input empty.
    payload = {
        "dsm_notify": True,
        "pushover_enabled": True,
        "pushover_api_token": "",
        "pushover_user_key": "",
        "pushover_devices": "iphone",
    }
    res = await client.post("/api/settings/notifications", json=payload)
    assert res.status_code == 200

    stored = config_store.get_notifications()
    assert stored["dsm_notify"] is True
    assert stored["pushover_api_token"] == "keep-me-token"
    assert stored["pushover_user_key"] == "keep-me-user"
    assert stored["pushover_devices"] == "iphone"


def test_pushover_respects_config_store_toggle(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "_CONFIG_FILE", tmp_path / "config.yaml")
    # Disabled: nothing should be sent.
    config_store.save_notifications({
        "dsm_notify": False,
        "pushover_enabled": False,
        "pushover_api_token": "token",
        "pushover_user_key": "user",
        "pushover_devices": "",
    })

    called = {"n": 0}
    def _fake_urlopen(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("urlopen must not be called when Pushover is disabled")
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    notification.send_pushover_notification("t", "m")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_test_endpoint_rejects_unknown_backend(client):
    res = await client.post("/api/settings/notifications/test", json={"backend": "email"})
    # Pydantic rejects the Literal with 422 before the handler runs.
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_test_endpoint_dsm_reports_missing_binary(client, monkeypatch):
    # Ensure the binary check returns False regardless of the host.
    monkeypatch.setattr(notification, "_binary_available", lambda: False)
    res = await client.post("/api/settings/notifications/test", json={"backend": "dsm"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is False
    assert "synodsmnotify" in body["message"]


@pytest.mark.asyncio
async def test_test_endpoint_pushover_requires_credentials(client):
    config_store.save_notifications({
        "dsm_notify": False,
        "pushover_enabled": True,
        "pushover_api_token": "",
        "pushover_user_key": "",
        "pushover_devices": "",
    })
    res = await client.post("/api/settings/notifications/test", json={"backend": "pushover"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is False
    assert "Token" in body["message"] or "User-Key" in body["message"]


@pytest.mark.asyncio
async def test_test_endpoint_pushover_success(client, monkeypatch):
    config_store.save_notifications({
        "dsm_notify": False,
        "pushover_enabled": True,
        "pushover_api_token": "token-abc",
        "pushover_user_key": "user-xyz",
        "pushover_devices": "iphone",
    })

    class _FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    captured = {}
    def _fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _FakeResp()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    res = await client.post("/api/settings/notifications/test", json={"backend": "pushover"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert "pushover.net" in captured["url"]
    assert b"token-abc" in captured["body"]
    assert b"user-xyz" in captured["body"]
    assert b"iphone" in captured["body"]
