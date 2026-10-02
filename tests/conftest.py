"""Shared pytest fixtures."""

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _german_ui(monkeypatch):
    """Run tests in German regardless of a local UI_LANGUAGE.

    Tests that need English set ``settings.ui_language`` themselves.
    """
    monkeypatch.setattr(settings, "ui_language", "de")
