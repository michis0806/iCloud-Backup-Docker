"""Wrapper around pyicloud for iCloud authentication and API access."""

import logging
import re
from pathlib import Path

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException

from app.config import settings

log = logging.getLogger("icloud-backup")

# In-memory cache of active PyiCloudService instances keyed by apple_id
_sessions: dict[str, PyiCloudService] = {}

# Cache of trusted devices retrieved for 2SA SMS flow
_trusted_devices: dict[str, list[dict]] = {}


def _cookie_dir_for(apple_id: str) -> str:
    """Return a per-account cookie directory path."""
    safe_name = re.sub(r"[^\w]", "_", apple_id)
    path = settings.cookie_directory / safe_name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def authenticate(apple_id: str, password: str | None = None) -> dict:
    """Authenticate with iCloud and return status information.

    Returns a dict with:
        - status: "authenticated" | "requires_2fa" | "error"
        - message: human-readable status message
    """
    cookie_dir = _cookie_dir_for(apple_id)

    try:
        api = PyiCloudService(
            apple_id=apple_id,
            password=password,
            cookie_directory=cookie_dir,
            verify=True,
        )
    except PyiCloudFailedLoginException as exc:
        return {"status": "error", "message": f"Login fehlgeschlagen: {exc}"}
    except Exception as exc:
        return {"status": "error", "message": f"Verbindungsfehler: {exc}"}

    _sessions[apple_id] = api

    if api.requires_2fa:
        return {
            "status": "requires_2fa",
            "message": "Zwei-Faktor-Authentifizierung erforderlich. "
            "Bitte geben Sie den Code von Ihrem Apple-Gerät ein.",
        }

    if api.requires_2sa:
        return {
            "status": "requires_2fa",
            "message": "Zwei-Stufen-Authentifizierung erforderlich. "
            "Bitte fordern Sie einen Code per SMS an.",
        }

    return {
        "status": "authenticated",
        "message": "Erfolgreich angemeldet.",
    }


def submit_2fa_code(apple_id: str, code: str) -> dict:
    """Submit a 2FA code for the given account.

    Returns a dict with:
        - status: "authenticated" | "error"
        - message: human-readable status message
    """
    api = _sessions.get(apple_id)
    if api is None:
        return {
            "status": "error",
            "message": "Keine aktive Sitzung. Bitte melden Sie sich erneut an.",
        }

    try:
        if not api.validate_2fa_code(code):
            return {
                "status": "error",
                "message": "Ungültiger Code. Bitte versuchen Sie es erneut.",
            }
        api.trust_session()
    except Exception as exc:
        return {"status": "error", "message": f"2FA-Fehler: {exc}"}

    return {
        "status": "authenticated",
        "message": "Zwei-Faktor-Authentifizierung erfolgreich.",
    }


def get_trusted_devices(apple_id: str) -> list[dict]:
    """Return the list of trusted devices available for SMS verification."""
    api = _sessions.get(apple_id)
    if api is None:
        return []

    try:
        devices = api.trusted_devices
        _trusted_devices[apple_id] = devices
        return [
            {
                "index": i,
                "name": d.get("deviceName", "Unknown"),
                "phone": d.get("phoneNumber", ""),
            }
            for i, d in enumerate(devices)
        ]
    except Exception as exc:
        log.error("Fehler beim Abrufen der Geräte für %s: %s", apple_id, exc)
        return []


def send_sms_code(apple_id: str, device_index: int) -> dict:
    """Send an SMS verification code to the given trusted device."""
    api = _sessions.get(apple_id)
    if api is None:
        return {"success": False, "message": "Keine aktive Sitzung."}

    devices = _trusted_devices.get(apple_id, [])
    if device_index < 0 or device_index >= len(devices):
        return {"success": False, "message": "Ungültiges Gerät."}

    try:
        success = api.send_verification_code(devices[device_index])
        if success:
            return {"success": True, "message": "SMS-Code gesendet."}
        return {"success": False, "message": "SMS konnte nicht gesendet werden."}
    except Exception as exc:
        return {"success": False, "message": f"Fehler: {exc}"}


def submit_2sa_code(apple_id: str, device_index: int, code: str) -> dict:
    """Submit a 2SA (SMS) verification code.

    Returns a dict with:
        - status: "authenticated" | "error"
        - message: human-readable status message
    """
    api = _sessions.get(apple_id)
    if api is None:
        return {
            "status": "error",
            "message": "Keine aktive Sitzung. Bitte melden Sie sich erneut an.",
        }

    devices = _trusted_devices.get(apple_id, [])
    if device_index < 0 or device_index >= len(devices):
        return {"status": "error", "message": "Ungültiges Gerät."}

    try:
        if not api.validate_verification_code(devices[device_index], code):
            return {
                "status": "error",
                "message": "Ungültiger Code. Bitte versuchen Sie es erneut.",
            }
    except Exception as exc:
        return {"status": "error", "message": f"2SA-Fehler: {exc}"}

    return {
        "status": "authenticated",
        "message": "Zwei-Stufen-Authentifizierung erfolgreich.",
    }


def get_session(apple_id: str) -> PyiCloudService | None:
    """Return an active PyiCloudService session, attempting reconnection if needed."""
    api = _sessions.get(apple_id)
    if api is not None:
        return api

    # Try to reconnect using saved session tokens (no password needed)
    cookie_dir = _cookie_dir_for(apple_id)
    try:
        api = PyiCloudService(
            apple_id=apple_id,
            cookie_directory=cookie_dir,
            verify=True,
        )
        if not api.requires_2fa and not api.requires_2sa:
            _sessions[apple_id] = api
            return api
    except Exception:
        pass

    return None


def get_drive_folders(apple_id: str) -> list[dict]:
    """List top-level iCloud Drive folders for simple mode selection."""
    api = get_session(apple_id)
    if api is None:
        return []

    folders = []
    try:
        root = api.drive
        for child in root.dir():
            node = root[child]
            folders.append(
                {
                    "name": child,
                    "type": "folder" if node.type == "folder" else "file",
                    "size": getattr(node, "size", None),
                }
            )
    except Exception as exc:
        log.error("Fehler beim Abrufen der Drive-Ordner für %s: %s", apple_id, exc)

    return sorted(folders, key=lambda f: f["name"].lower())


def disconnect(apple_id: str) -> None:
    """Remove a session from the in-memory cache."""
    _sessions.pop(apple_id, None)
