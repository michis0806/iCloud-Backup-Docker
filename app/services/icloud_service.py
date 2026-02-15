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
    """Return the list of trusted devices/phone numbers available for SMS verification."""
    api = _sessions.get(apple_id)
    if api is None:
        return []

    # 2FA (HSA2): phone numbers come from auth data, not the old listDevices API
    if api.requires_2fa:
        try:
            phones = getattr(api, "_auth_data", {}).get("trustedPhoneNumbers", [])
            _trusted_devices[apple_id] = phones
            return [
                {
                    "index": i,
                    "name": f"SMS an {p.get('numberWithDialCode', 'Unbekannt')}",
                    "phone": p.get("numberWithDialCode", ""),
                }
                for i, p in enumerate(phones)
            ]
        except Exception as exc:
            log.error("Fehler beim Abrufen der Telefonnummern für %s: %s", apple_id, exc)
            return []

    # 2SA (legacy): use traditional trusted devices API
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
    """Send an SMS verification code to the given trusted device/phone number."""
    api = _sessions.get(apple_id)
    if api is None:
        return {"success": False, "message": "Keine aktive Sitzung."}

    devices = _trusted_devices.get(apple_id, [])
    if device_index < 0 or device_index >= len(devices):
        return {"success": False, "message": "Ungültiges Gerät."}

    # 2FA (HSA2): request SMS via Apple auth endpoint
    if api.requires_2fa:
        phone = devices[device_index]
        phone_id = phone.get("id")
        try:
            headers = api._get_auth_headers({"Accept": "application/json"})
            data = {"phoneNumber": {"id": phone_id}, "mode": "sms"}
            resp = api.session.put(
                f"{api._auth_endpoint}/verify/phone",
                json=data,
                headers=headers,
            )
            # Update auth_data so validate_2fa_code() uses SMS mode
            resp_json = resp.json()
            api._auth_data.update(resp_json)
            api._auth_data["mode"] = "sms"
            # Ensure trustedPhoneNumber is set for _validate_sms_code()
            if "trustedPhoneNumber" not in api._auth_data:
                api._auth_data["trustedPhoneNumber"] = phone
            return {"success": True, "message": "SMS-Code gesendet."}
        except Exception as exc:
            return {"success": False, "message": f"Fehler: {exc}"}

    # 2SA (legacy): use traditional send_verification_code
    try:
        success = api.send_verification_code(devices[device_index])
        if success:
            return {"success": True, "message": "SMS-Code gesendet."}
        return {"success": False, "message": "SMS konnte nicht gesendet werden."}
    except Exception as exc:
        return {"success": False, "message": f"Fehler: {exc}"}


def submit_2sa_code(apple_id: str, device_index: int, code: str) -> dict:
    """Submit a 2SA/2FA SMS verification code.

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

    # 2FA (HSA2): use validate_2fa_code which handles SMS mode internally
    if api.requires_2fa:
        try:
            if not api.validate_2fa_code(code):
                return {
                    "status": "error",
                    "message": "Ungültiger Code. Bitte versuchen Sie es erneut.",
                }
        except Exception as exc:
            return {"status": "error", "message": f"2FA-Fehler: {exc}"}

        return {
            "status": "authenticated",
            "message": "Zwei-Faktor-Authentifizierung erfolgreich.",
        }

    # 2SA (legacy): use traditional validation
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
