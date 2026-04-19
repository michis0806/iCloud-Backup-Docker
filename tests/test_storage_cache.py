"""Tests for the iCloud storage cache."""

from unittest.mock import patch

import pytest

from app.config import settings
from app.services import storage_cache


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_path", tmp_path)
    yield tmp_path


def test_cache_roundtrip(temp_config):
    apple_id = "alice@icloud.com"
    assert storage_cache.load_cache(apple_id) is None

    payload = {
        "used_bytes": 1234,
        "total_bytes": 5000,
        "available_bytes": 3766,
        "used_percent": 24.6,
        "quota_over": False,
        "media": [{"key": "photos", "label": "Fotos", "color": "#fff", "usage_bytes": 1234}],
    }
    storage_cache.save_cache(apple_id, payload)

    loaded = storage_cache.load_cache(apple_id)
    assert loaded["used_bytes"] == 1234
    assert loaded["media"][0]["label"] == "Fotos"
    assert "fetched_at" in loaded and loaded["fetched_at"]


def test_delete_cache(temp_config):
    apple_id = "bob@icloud.com"
    storage_cache.save_cache(apple_id, {"used_bytes": 1})
    assert storage_cache.load_cache(apple_id) is not None
    storage_cache.delete_cache(apple_id)
    assert storage_cache.load_cache(apple_id) is None


def test_refresh_updates_cache(temp_config):
    apple_id = "charlie@icloud.com"
    fresh = {"used_bytes": 7, "total_bytes": 10, "media": []}
    with patch("app.services.storage_cache.icloud_service.get_storage_usage", return_value=fresh):
        result = storage_cache.refresh(apple_id)
    assert result is not None
    assert result["used_bytes"] == 7
    # Second call with None must keep previous data.
    with patch("app.services.storage_cache.icloud_service.get_storage_usage", return_value=None):
        assert storage_cache.refresh(apple_id) is None
    still = storage_cache.load_cache(apple_id)
    assert still is not None
    assert still["used_bytes"] == 7


def test_cache_path_is_filesystem_safe(temp_config):
    # '@' and '/' are sanitised to avoid path traversal or unexpected subdirs.
    path = storage_cache._cache_path("alice@icloud.com/../evil")
    assert ".." not in path.name
    assert path.parent == temp_config
