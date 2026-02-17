"""Tests for FastAPI API endpoints."""

import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock
from httpx import AsyncClient, ASGITransport

from app.main import app
from app import config_store


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Create a test client with a temporary YAML config."""
    # Point config_store to a temp file
    monkeypatch.setattr(config_store, "_CONFIG_FILE", tmp_path / "config.yaml")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, client):
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert "build" in data
        assert {"version", "commit", "build_date"}.issubset(data["build"].keys())


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
