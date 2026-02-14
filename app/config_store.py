"""YAML-based configuration store for accounts and backup configs.

Replaces the SQLite/SQLAlchemy persistence layer with a simple
human-readable YAML file at /config/config.yaml.
"""

import enum
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

import yaml

from app.config import settings

log = logging.getLogger("icloud-backup")


_lock = threading.Lock()
_CONFIG_FILE: Path = settings.config_path / "config.yaml"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read() -> dict:
    """Read the YAML config file and return its contents as a dict."""
    if not _CONFIG_FILE.exists():
        return {"accounts": []}
    try:
        text = _CONFIG_FILE.read_text()
        # Strip !!python/ tags that yaml.safe_load cannot handle.
        # These are written by yaml.dump() for enum/object values.
        text = re.sub(r"!!python/\S+\n\s*- ", "", text)
        data = yaml.safe_load(text) or {}
    except Exception:
        log.error("Fehler beim Lesen der Konfigurationsdatei %s", _CONFIG_FILE, exc_info=True)
        data = {}
    if "accounts" not in data:
        data["accounts"] = []
    return data


def _sanitize(obj):
    """Recursively convert enum values to plain strings for YAML serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, enum.Enum):
        return obj.value
    return obj


def _write(data: dict) -> None:
    """Atomically write *data* to the YAML config file."""
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".yaml.tmp")
    clean = _sanitize(data)
    tmp.write_text(yaml.safe_dump(clean, default_flow_style=False, allow_unicode=True, sort_keys=False))
    tmp.rename(_CONFIG_FILE)


def _find_account(data: dict, apple_id: str) -> dict | None:
    for acc in data["accounts"]:
        if acc["apple_id"] == apple_id:
            return acc
    return None


def _default_backup() -> dict:
    return {
        "backup_drive": False,
        "backup_photos": False,
        "drive_config_mode": "simple",
        "drive_folders_simple": None,
        "drive_folders_advanced": None,
        "photos_include_family": False,
        "exclusions": None,
        "destination": "",
        "schedule_enabled": False,
        "schedule_cron": None,
        "last_backup_status": "idle",
        "last_backup_at": None,
        "last_backup_message": None,
        "last_backup_stats": None,
    }


# ---------------------------------------------------------------------------
# Public API – accounts
# ---------------------------------------------------------------------------

def list_accounts() -> list[dict]:
    with _lock:
        data = _read()
    return [
        {
            "apple_id": acc["apple_id"],
            "status": acc.get("status", "pending"),
            "status_message": acc.get("status_message"),
        }
        for acc in data["accounts"]
    ]


def get_account(apple_id: str) -> dict | None:
    with _lock:
        data = _read()
        acc = _find_account(data, apple_id)
    if acc is None:
        return None
    return {
        "apple_id": acc["apple_id"],
        "status": acc.get("status", "pending"),
        "status_message": acc.get("status_message"),
    }


def add_account(apple_id: str, status: str = "pending", status_message: str | None = None) -> dict:
    with _lock:
        data = _read()
        if _find_account(data, apple_id) is not None:
            raise ValueError("Account existiert bereits.")
        acc = {
            "apple_id": apple_id,
            "status": status,
            "status_message": status_message,
            "backup": _default_backup(),
        }
        data["accounts"].append(acc)
        _write(data)
    return {
        "apple_id": acc["apple_id"],
        "status": acc["status"],
        "status_message": acc["status_message"],
    }


def update_account_status(apple_id: str, status: str, status_message: str | None = None) -> dict | None:
    with _lock:
        data = _read()
        acc = _find_account(data, apple_id)
        if acc is None:
            return None
        acc["status"] = status
        acc["status_message"] = status_message
        _write(data)
    return {
        "apple_id": acc["apple_id"],
        "status": acc["status"],
        "status_message": acc["status_message"],
    }


def delete_account(apple_id: str) -> bool:
    with _lock:
        data = _read()
        before = len(data["accounts"])
        data["accounts"] = [a for a in data["accounts"] if a["apple_id"] != apple_id]
        if len(data["accounts"]) == before:
            return False
        _write(data)
    return True


# ---------------------------------------------------------------------------
# Public API – backup config
# ---------------------------------------------------------------------------

def get_backup_config(apple_id: str) -> dict | None:
    with _lock:
        data = _read()
        acc = _find_account(data, apple_id)
    if acc is None:
        return None
    backup = acc.get("backup") or _default_backup()
    return {**backup, "apple_id": apple_id}


def save_backup_config(apple_id: str, config: dict) -> dict | None:
    with _lock:
        data = _read()
        acc = _find_account(data, apple_id)
        if acc is None:
            return None
        if "backup" not in acc:
            acc["backup"] = _default_backup()
        for key in (
            "backup_drive", "backup_photos", "drive_config_mode",
            "drive_folders_simple", "drive_folders_advanced",
            "photos_include_family", "exclusions", "destination",
            "schedule_enabled", "schedule_cron",
        ):
            if key in config:
                acc["backup"][key] = config[key]
        # Auto-generate destination if empty
        if not acc["backup"].get("destination"):
            acc["backup"]["destination"] = apple_id.replace("@", "_at_").replace(".", "_")
        _write(data)
    return {**acc["backup"], "apple_id": apple_id}


def update_backup_status(
    apple_id: str,
    status: str,
    message: str | None = None,
    stats: dict | None = None,
    at: str | None = None,
) -> None:
    with _lock:
        data = _read()
        acc = _find_account(data, apple_id)
        if acc is None:
            return
        if "backup" not in acc:
            acc["backup"] = _default_backup()
        acc["backup"]["last_backup_status"] = status
        if message is not None:
            acc["backup"]["last_backup_message"] = message
        if stats is not None:
            acc["backup"]["last_backup_stats"] = stats
        if at is not None:
            acc["backup"]["last_backup_at"] = at
        _write(data)


# ---------------------------------------------------------------------------
# Public API – scheduled configs
# ---------------------------------------------------------------------------

def list_scheduled_configs() -> list[dict]:
    """Return all backup configs that have scheduling enabled."""
    with _lock:
        data = _read()
    result = []
    for acc in data["accounts"]:
        backup = acc.get("backup") or {}
        if backup.get("schedule_enabled"):
            result.append({
                "apple_id": acc["apple_id"],
                "status": acc.get("status", "pending"),
                **backup,
            })
    return result
