"""Tests for etag cache persistence."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from app.services.backup_service import _load_cache, _save_cache, _cache_path


@pytest.fixture
def tmp_config(tmp_path):
    with patch("app.services.backup_service.settings") as mock_settings:
        mock_settings.config_path = tmp_path
        mock_settings.backup_path = tmp_path / "backups"
        yield tmp_path


class TestCachePath:
    def test_generates_path(self, tmp_config):
        path = _cache_path("user_at_icloud_com", "Documents")
        assert "Documents" in str(path)
        assert "user_at_icloud_com" in str(path)
        assert str(path).endswith(".json")

    def test_slashes_replaced(self, tmp_config):
        path = _cache_path("dest", "path/with/slashes")
        assert "/" not in path.name or str(path).count("/") == str(tmp_config).count("/") + 1


class TestLoadCache:
    def test_returns_empty_when_missing(self, tmp_config):
        result = _load_cache("nonexistent", "folder")
        assert result == {}

    def test_loads_valid_json(self, tmp_config):
        cache_data = {"Documents/Sub": "etag123", "Documents/Other": "etag456"}
        path = _cache_path("dest", "Documents")
        path.write_text(json.dumps(cache_data))

        result = _load_cache("dest", "Documents")
        assert result == cache_data

    def test_handles_corrupted_file(self, tmp_config):
        path = _cache_path("dest", "Documents")
        path.write_text("not valid json {{{")

        result = _load_cache("dest", "Documents")
        assert result == {}


class TestSaveCache:
    def test_saves_and_reloads(self, tmp_config):
        cache_data = {"folder/sub": "etag_abc"}
        _save_cache("dest", "TestFolder", cache_data)

        loaded = _load_cache("dest", "TestFolder")
        assert loaded == cache_data

    def test_overwrites_existing(self, tmp_config):
        _save_cache("dest", "Folder", {"old": "data"})
        _save_cache("dest", "Folder", {"new": "data"})

        loaded = _load_cache("dest", "Folder")
        assert loaded == {"new": "data"}
