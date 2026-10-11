"""Tests for backup progress tracking."""

import pytest

from app.services.backup_service import get_progress, _set_progress, _clear_progress
from app.routers import backup as backup_router
from app import config_store


class TestProgress:
    def test_no_progress_initially(self):
        assert get_progress(9999) is None

    def test_set_and_get(self):
        _set_progress(100, {"phase": "drive", "downloaded": 5})
        result = get_progress(100)
        assert result is not None
        assert result["phase"] == "drive"
        assert result["downloaded"] == 5
        _clear_progress(100)

    def test_clear(self):
        _set_progress(101, {"phase": "photos"})
        _clear_progress(101)
        assert get_progress(101) is None

    def test_clear_nonexistent(self):
        # Should not raise
        _clear_progress(99999)

    def test_overwrite(self):
        _set_progress(102, {"phase": "drive", "downloaded": 1})
        _set_progress(102, {"phase": "drive", "downloaded": 2})
        result = get_progress(102)
        assert result["downloaded"] == 2
        _clear_progress(102)


class TestBackupStatusLifecycle:
    @pytest.mark.asyncio
    async def test_status_poll_does_not_mark_precheck_as_interrupted(self, monkeypatch):
        apple_id = "precheck@example.com"
        running_config = {
            "last_backup_status": "running",
            "last_backup_message": None,
            "last_backup_at": None,
            "last_backup_started_at": "2026-10-11T10:00:00+00:00",
            "last_backup_duration_seconds": None,
            "last_backup_stats": None,
        }
        update_status = []
        monkeypatch.setattr(config_store, "get_backup_config", lambda _id: running_config)
        monkeypatch.setattr(
            config_store,
            "update_backup_status",
            lambda *args, **kwargs: update_status.append((args, kwargs)),
        )
        monkeypatch.setattr(backup_router.backup_service, "get_progress", lambda _id: None)

        response = await backup_router.get_backup_status(apple_id)

        assert response["status"] == "running"
        assert response["message"] is None
        assert update_status == []
