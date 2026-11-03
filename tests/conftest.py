"""Shared pytest fixtures."""

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _german_ui(monkeypatch):
    """Run tests in German regardless of a local UI_LANGUAGE.

    Tests that need English set ``settings.ui_language`` themselves.
    """
    monkeypatch.setattr(settings, "ui_language", "de")


@pytest.fixture(autouse=True)
def _isolated_data_dirs(tmp_path, monkeypatch):
    """Point all data paths into tmp_path so tests never touch /config etc.

    CI runners are not root and have no /config; tests that create session
    directories would otherwise fail there (or litter the host when run as
    root). Tests that need specific paths override these themselves.
    """
    data = tmp_path / "_data"
    for name in ("config_path", "backup_path", "archive_path"):
        path = data / name.removesuffix("_path")
        path.mkdir(parents=True)
        monkeypatch.setattr(settings, name, path)
    cookies = data / "config" / "sessions"
    cookies.mkdir()
    monkeypatch.setattr(settings, "cookie_directory", cookies)
