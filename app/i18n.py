"""Minimal message catalog for the UI language (UI_LANGUAGE = de | en | fr | it | es).

Catalogs live in app/locales/<lang>.json as flat ``key -> message`` maps with
``{placeholder}`` fields. Keys starting with ``js.`` are also sent to the
browser (see ``js_messages``). German is the fallback for missing keys.
"""

import json
from functools import lru_cache
from pathlib import Path

from app.config import settings

_LOCALES_DIR = Path(__file__).parent / "locales"
_FALLBACK = "de"
_JS_PREFIX = "js."


@lru_cache(maxsize=None)
def _catalog(lang: str) -> dict[str, str]:
    return json.loads((_LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def get_language() -> str:
    return settings.ui_language


def t(key: str, **kwargs) -> str:
    """Return the message for ``key`` in the active language.

    Placeholders are only substituted when keyword arguments are passed, so
    messages without arguments may contain literal braces.
    """
    text = _catalog(get_language()).get(key)
    if text is None:
        text = _catalog(_FALLBACK).get(key, key)
    return text.format(**kwargs) if kwargs else text


def js_messages() -> dict[str, str]:
    """Catalog subset the frontend needs, in the active language."""
    merged = {**_catalog(_FALLBACK), **_catalog(get_language())}
    return {k: v for k, v in merged.items() if k.startswith(_JS_PREFIX)}
