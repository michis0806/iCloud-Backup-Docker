"""Persistent cache for iCloud storage usage data.

The dashboard displays per-account storage breakdown (photos, drive, backups,
mail, ...). Fetching this from Apple on every page load is slow and can hit
rate limits, so we cache the result to disk and refresh it after each backup.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.services import icloud_service

log = logging.getLogger("icloud-backup")

_lock = threading.Lock()


def _cache_path(apple_id: str) -> Path:
    safe = (
        apple_id.replace("@", "_at_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace("..", "_")
    )
    return settings.config_path / f".icloud-storage-cache-{safe}.json"


def load_cache(apple_id: str) -> dict | None:
    """Return cached storage data for *apple_id* or ``None`` when missing/invalid."""
    path = _cache_path(apple_id)
    if not path.exists():
        return None
    try:
        with _lock:
            raw = json.loads(path.read_text())
    except Exception:
        log.warning("Speicher-Cache für %s konnte nicht gelesen werden", apple_id, exc_info=True)
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    data = dict(data)
    data["fetched_at"] = raw.get("fetched_at")
    return data


def save_cache(apple_id: str, data: dict) -> None:
    """Persist storage usage *data* for *apple_id*."""
    path = _cache_path(apple_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "apple_id": apple_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    tmp = path.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(path)


def delete_cache(apple_id: str) -> None:
    """Remove the storage cache file for *apple_id* (used on account deletion)."""
    path = _cache_path(apple_id)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        log.debug("Speicher-Cache für %s konnte nicht gelöscht werden", apple_id, exc_info=True)


def refresh(apple_id: str) -> dict | None:
    """Fetch fresh storage usage from iCloud and update the cache.

    Returns the fetched data (with ``fetched_at``) or ``None`` when the API
    call failed. Existing cache is kept on failure.
    """
    data = icloud_service.get_storage_usage(apple_id)
    if data is None:
        log.info("iCloud-Speicherinfo für %s nicht abrufbar – Cache bleibt unverändert", apple_id)
        return None
    save_cache(apple_id, data)
    log.info("iCloud-Speicherinfo für %s aktualisiert", apple_id)
    return load_cache(apple_id)
