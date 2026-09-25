"""Tests for FastAPI API endpoints."""

import errno
import os
import threading

import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock
from httpx import AsyncClient, ASGITransport

from app.main import app
from app import config_store
from app.auth import _COOKIE_NAME, create_session_cookie
from app.services import icloud_service
from tests.test_apple_auth import api


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Create a test client with a temporary YAML config."""
    # Point config_store to a temp file
    monkeypatch.setattr(config_store, "_CONFIG_FILE", tmp_path / "config.yaml")

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={_COOKIE_NAME: create_session_cookie()},
    ) as client:
        yield client


class TestHealthEndpoint:
    @pytest.fixture(autouse=True)
    def storage_paths(self, tmp_path, monkeypatch):
        from app.config import settings

        paths = {}
        for name in ("backup_path", "config_path", "archive_path"):
            path = tmp_path / name
            path.mkdir()
            monkeypatch.setattr(settings, name, path)
            paths[name] = path
        return paths

    @pytest.mark.asyncio
    async def test_health(self, client):
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert "build" in data
        assert {"version", "commit", "build_date"}.issubset(data["build"].keys())

    @pytest.mark.asyncio
    async def test_health_reports_unreachable_mount(self, client, storage_paths):
        backup_path = str(storage_paths["backup_path"])
        real_scandir = os.scandir

        def scandir(path):
            if str(path) == backup_path:
                raise OSError(errno.EHOSTDOWN, "Host is down", backup_path)
            return real_scandir(path)

        with patch("app.main.os.scandir", side_effect=scandir):
            res = await client.get("/health")

        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "error"
        assert data["storage"] == {backup_path: f"Host is down (errno {errno.EHOSTDOWN})"}

    @pytest.mark.asyncio
    async def test_health_times_out_on_hanging_mount(self, client, monkeypatch):
        import app.main as main

        release = threading.Event()

        def hanging_check():
            release.wait(5)
            return {}

        monkeypatch.setattr(main, "_check_storage", hanging_check)
        monkeypatch.setattr(main, "_STORAGE_CHECK_TIMEOUT", 0.05)
        monkeypatch.setattr(main, "_storage_check_task", None)
        try:
            res = await client.get("/health")
            assert res.status_code == 503
            assert "storage" in res.json()["storage"]
            pending = main._storage_check_task

            # A second probe reuses the still-running check.
            await client.get("/health")
            assert main._storage_check_task is pending
        finally:
            release.set()


class TestAccountsAPI:
    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        res = await client.get("/api/accounts")
        assert res.status_code == 200
        assert res.json() == []

    @pytest.mark.asyncio
    @patch("app.routers.accounts.icloud_service.authenticate")
    async def test_add_account(self, mock_auth, client):
        mock_auth.return_value = {
            "status": "requires_2fa",
            "message": "2FA erforderlich",
        }
        res = await client.post(
            "/api/accounts",
            json={"apple_id": "test@icloud.com", "password": "secret"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["apple_id"] == "test@icloud.com"
        assert data["status"] == "requires_2fa"

    @pytest.mark.asyncio
    @patch("app.routers.accounts.icloud_service.authenticate")
    async def test_add_duplicate(self, mock_auth, client):
        mock_auth.return_value = {"status": "authenticated", "message": "OK"}
        await client.post(
            "/api/accounts",
            json={"apple_id": "dupe@icloud.com", "password": "pw"},
        )
        res = await client.post(
            "/api/accounts",
            json={"apple_id": "dupe@icloud.com", "password": "pw"},
        )
        assert res.status_code == 400

    @pytest.mark.asyncio
    @patch("app.routers.accounts.icloud_service.disconnect")
    @patch("app.routers.accounts.icloud_service.authenticate")
    async def test_delete_account(self, mock_auth, mock_disconnect, client):
        mock_auth.return_value = {"status": "authenticated", "message": "OK"}
        await client.post(
            "/api/accounts",
            json={"apple_id": "del@icloud.com", "password": "pw"},
        )

        res = await client.delete("/api/accounts/del@icloud.com")
        assert res.status_code == 200

        list_res = await client.get("/api/accounts")
        assert len(list_res.json()) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["push", "sms"])
async def test_reauth_waits_for_channel_then_persists_success(client, api, monkeypatch, channel):
    apple_id = "test@example.com"
    config_store.add_account(apple_id, status="requires_2fa", status_message="Expired")
    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    monkeypatch.setattr(icloud_service, "_trusted_devices", {})
    base = f"/api/accounts/{apple_id}"

    reconnect = await client.post(base + "/reconnect", json={})
    assert reconnect.json()["status"] == "requires_2fa"
    api.session.get.assert_not_called()
    api.session.put.assert_not_called()

    if channel == "push":
        sent = await client.post(base + "/2fa/push", json={})
        verified = await client.post(base + "/2fa", json={"code": "123456"})
        api.session.put.assert_not_called()
    else:
        devices = await client.get(base + "/2fa/devices")
        assert devices.json()[0]["index"] == 0
        sent = await client.post(base + "/2fa/sms", json={"device_index": 0})
        verified = await client.post(base + "/2sa", json={"device_index": 0, "code": "123456"})
        api.session.get.assert_not_called()

    assert sent.json()["success"]
    assert verified.json()["status"] == "authenticated"
    assert config_store.get_account(apple_id)["status"] == "authenticated"
    api.trust_session.assert_called_once()


class TestLogsAPI:
    @pytest.mark.asyncio
    async def test_get_logs(self, client):
        res = await client.get("/api/logs")
        assert res.status_code == 200
        assert isinstance(res.json(), list)


class TestProgressAPI:
    @pytest.mark.asyncio
    async def test_no_progress(self, client):
        res = await client.get("/api/backup/progress/nonexistent@icloud.com")
        assert res.status_code == 200
        assert res.json()["running"] is False
