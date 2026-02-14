"""Tests for FastAPI API endpoints."""

import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import init_db, engine, Base


@pytest_asyncio.fixture
async def client():
    """Create a test client with a fresh in-memory database."""
    # Use in-memory SQLite for tests
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    test_session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with test_session() as session:
            yield session

    from app.database import get_db
    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await test_engine.dispose()


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, client):
        res = await client.get("/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}


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
        create_res = await client.post(
            "/api/accounts",
            json={"apple_id": "del@icloud.com", "password": "pw"},
        )
        account_id = create_res.json()["id"]

        res = await client.delete(f"/api/accounts/{account_id}")
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
        res = await client.get("/api/backup/progress/9999")
        assert res.status_code == 200
        assert res.json()["running"] is False
