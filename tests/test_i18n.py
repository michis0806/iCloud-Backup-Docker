"""Tests for the UI language catalogs and the UI_LANGUAGE switch."""

import json
import re
from pathlib import Path
from typing import get_args

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import config_store, i18n
from app.auth import _COOKIE_NAME, create_session_cookie
from app.config import Settings, settings
from app.main import app

_LOCALES = Path(i18n.__file__).parent / "locales"
_GERMAN = re.compile(
    r"[äöÄÖÜß]|(?<![gG])ü|\b(Fehler|Einstellungen|Konto|Verbindung|Passwort|Speichern|"
    r"Abbrechen|Sicherung|Datei|Dateien|Ordner|Bitte|Wird|nicht)\b"
)


def _visible(html):
    return re.sub(r"^\s*//.*$", "", html, flags=re.MULTILINE)


def _load(lang):
    return json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


def _placeholders(text):
    return sorted(re.findall(r"\{(\w+)\}", text))


_LANGUAGES = sorted(p.stem for p in _LOCALES.glob("*.json"))
_TRANSLATIONS = [lang for lang in _LANGUAGES if lang != "de"]


def _log_specifiers(text):
    return re.findall(r"%(?:\.\d+)?[sdrf]", text)


def test_every_supported_language_has_a_catalog():
    from app.config import Settings

    assert sorted(get_args(Settings.model_fields["ui_language"].annotation)) == _LANGUAGES


@pytest.mark.parametrize("lang", _TRANSLATIONS)
def test_catalogs_have_identical_keys(lang):
    assert list(_load(lang)) == list(_load("de"))


@pytest.mark.parametrize("lang", _TRANSLATIONS)
def test_catalogs_have_identical_placeholders(lang):
    de, other = _load("de"), _load(lang)
    mismatched = [
        k for k in de
        if _placeholders(de[k]) != _placeholders(other[k])
        or _log_specifiers(de[k]) != _log_specifiers(other[k])
    ]
    assert mismatched == []


@pytest.mark.parametrize("lang", _TRANSLATIONS)
def test_translated_catalog_has_no_german(lang):
    hits = {k: v for k, v in _load(lang).items() if _GERMAN.search(v)}
    assert hits == {}


def test_invalid_language_is_rejected():
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(ui_language="xx")
    assert Settings(ui_language=" EN ").ui_language == "en"


def test_t_formats_placeholders_and_falls_back_to_key(monkeypatch):
    monkeypatch.setattr(settings, "ui_language", "en")
    assert i18n.t("js.date_locale") == "en-US"
    assert i18n.t("no.such.key") == "no.such.key"


def _client(tmp_path, monkeypatch, lang):
    monkeypatch.setattr(config_store, "_CONFIG_FILE", tmp_path / "config.yaml")
    monkeypatch.setattr(settings, "ui_language", lang)
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={_COOKIE_NAME: create_session_cookie()},
    )


@pytest_asyncio.fixture
async def en_client(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch, "en") as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", _TRANSLATIONS)
@pytest.mark.parametrize("path", ["/", "/accounts/a@example.com", "/logs", "/settings"])
async def test_pages_render_without_german(tmp_path, monkeypatch, lang, path):
    async with _client(tmp_path, monkeypatch, lang) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert f'<html lang="{lang}"' in response.text
    assert not _GERMAN.search(_visible(response.text))


@pytest.mark.asyncio
async def test_login_error_is_english(en_client):
    response = await en_client.post("/login", data={"password": "wrong"})
    assert response.status_code == 401
    assert not _GERMAN.search(response.text)


@pytest.mark.asyncio
async def test_api_error_is_english(en_client):
    response = await en_client.get("/api/accounts/unknown@example.com/2fa/devices")
    assert response.status_code == 404
    assert not _GERMAN.search(response.json()["detail"])


@pytest.mark.asyncio
async def test_german_is_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "_CONFIG_FILE", tmp_path / "config.yaml")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={_COOKIE_NAME: create_session_cookie()},
    ) as client:
        page = await client.get("/")
        detail = (await client.get("/api/accounts/unknown@example.com/2fa/devices")).json()["detail"]
    assert Settings.model_fields["ui_language"].default == "de"
    assert '<html lang="de"' in page.text
    assert detail == _load("de")["api.accounts.not_found"]
