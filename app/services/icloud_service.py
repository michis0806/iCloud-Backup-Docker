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

# Cache of detected CloudKit ownerRecordName per apple_id.
# Populated by get_drive_folders() and consumed by backup_service.
_user_records: dict[str, str] = {}


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


def get_user_record(apple_id: str) -> str | None:
    """Return the cached CloudKit ownerRecordName for *apple_id*, or None."""
    return _user_records.get(apple_id)



def get_drive_folders(apple_id: str) -> list[dict]:
    """List top-level iCloud Drive folders for simple mode selection.

    Each dict contains:
      - name: folder/file name
      - type: "folder" or "file"
      - size: file size or None
      - shared_not_owned: True if folder is shared *with* this user by
        someone else (cannot be downloaded via the standard API)
    """
    api = get_session(apple_id)
    if api is None:
        return []

    folders = []
    try:
        root = api.drive
        children_names = root.dir()

        # ---- Diagnostic: dump distinguishing fields per shared child ----
        # We need to find a reliable signal for "own share" vs "foreign share"
        # without knowing the user's CloudKit record. Log zone, drivewsid,
        # and all shareID sub-fields so we can identify the right heuristic.
        share_info: dict[str, dict] = {}  # child_name → {owner, node_data}
        for child_name in children_names:
            node = root[child_name]
            sid = node.data.get("shareID")
            if isinstance(sid, dict):
                owner = sid.get("zoneID", {}).get("ownerRecordName", "")
                share_info[child_name] = {
                    "owner": owner,
                    "share_id": sid,
                    "node": node,
                }
                # Log ALL potentially distinguishing fields
                log.info(
                    "DIAG Ordner '%s': zone=%s, drivewsid=%s, "
                    "shareID.zoneID=%s, shareID-keys=%s, "
                    "node-data-keys=%s",
                    child_name,
                    node.data.get("zone"),
                    node.data.get("drivewsid"),
                    sid.get("zoneID"),
                    list(sid.keys()),
                    [k for k in node.data.keys()
                     if k not in ("items", "name", "extension")],
                )

        # ---- Infer user record ----
        # Strategy: look at the 'zone' field on each shared node.
        # Own shared folders use "com.apple.CloudDocs" as zone,
        # foreign shared folders use a different zone name.
        # If that doesn't work, fall back to majority vote.
        user_record = None
        if share_info:
            from collections import Counter
            owner_counts: Counter = Counter(
                info["owner"] for info in share_info.values() if info["owner"]
            )
            if owner_counts:
                most_common, count = owner_counts.most_common(1)[0]
                total = sum(owner_counts.values())
                log.info(
                    "Mehrheitsentscheid: %s (%d/%d); Verteilung: %s",
                    most_common, count, total, dict(owner_counts),
                )
                user_record = most_common
                _user_records[apple_id] = most_common

        # ---- Build folder list ----
        for child_name in children_names:
            node = root[child_name]
            shared_not_owned = False

            info = share_info.get(child_name)
            if info and info["owner"]:
                owner = info["owner"]
                if user_record:
                    shared_not_owned = owner != user_record
                else:
                    shared_not_owned = True
                log.info(
                    "Ordner '%s': ownerRecordName=%s → %s",
                    child_name,
                    owner,
                    "Fremdfreigabe" if shared_not_owned else "eigene Freigabe",
                )

            folders.append(
                {
                    "name": child_name,
                    "type": "folder" if node.type == "folder" else "file",
                    "size": getattr(node, "size", None),
                    "shared_not_owned": shared_not_owned,
                }
            )
    except Exception as exc:
        log.error("Fehler beim Abrufen der Drive-Ordner für %s: %s", apple_id, exc)

    return sorted(folders, key=lambda f: f["name"].lower())


def get_photo_libraries(apple_id: str) -> list[dict]:
    """Return available photo libraries for the given account.

    Returns a list of dicts::

        [
            {"id": "PrimarySync", "type": "primary", "name": "Eigene Mediathek"},
            {"id": "SharedSync-XXXX-...", "type": "shared", "name": "Geteilte Mediathek"},
        ]
    """
    api = get_session(apple_id)
    if api is None:
        return []

    result = []
    try:
        libraries = api.photos.libraries
        for zone_name in libraries:
            if zone_name == "root":
                # "root" is a pyicloud alias for PrimarySync – skip, always present
                continue
            if zone_name == "shared":
                # "shared" is the SharedPhotoStreamLibrary (shared albums, not the
                # iCloud Shared Library) – skip, handled separately
                continue
            if zone_name == "PrimarySync":
                result.append({
                    "id": "PrimarySync",
                    "type": "primary",
                    "name": "Eigene Mediathek",
                })
            elif zone_name.startswith("SharedSync-"):
                result.append({
                    "id": zone_name,
                    "type": "shared",
                    "name": "Geteilte Mediathek",
                })
    except Exception as exc:
        log.error("Fehler beim Abrufen der Foto-Bibliotheken für %s: %s", apple_id, exc)

    # If we couldn't enumerate, at least return the primary library
    if not result:
        result.append({
            "id": "PrimarySync",
            "type": "primary",
            "name": "Eigene Mediathek",
        })

    return result


def disconnect(apple_id: str) -> None:
    """Remove a session from the in-memory cache."""
    _sessions.pop(apple_id, None)
