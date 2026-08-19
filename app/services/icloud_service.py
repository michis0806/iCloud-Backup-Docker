"""Wrapper around pyicloud for iCloud authentication and API access."""

import json
import logging
import re
from pathlib import Path

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException

from app import config_store
from app.config import settings

log = logging.getLogger("icloud-backup")

# In-memory cache of active PyiCloudService instances keyed by apple_id
_sessions: dict[str, PyiCloudService] = {}

# Cache of trusted devices / phone numbers retrieved for SMS verification
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


def _auth_endpoint_for(api: PyiCloudService) -> str | None:
    """Return the Apple auth endpoint across pyicloud versions."""
    return getattr(api, "_auth_endpoint", None) or getattr(api, "AUTH_ENDPOINT", None)


def _auth_headers(api: PyiCloudService, overrides: dict | None = None) -> dict:
    """Build Apple auth headers including session-specific challenge headers."""
    overrides = overrides or {}
    try:
        headers = api._get_auth_headers(overrides)
    except Exception:
        headers = dict(overrides)

    session_data = getattr(api, "session_data", {}) or {}
    if session_data.get("scnt"):
        headers["scnt"] = session_data["scnt"]
    if session_data.get("session_id"):
        headers["X-Apple-ID-Session-Id"] = session_data["session_id"]
    return headers


def _cache_auth_options(api: PyiCloudService, auth_options: dict | None) -> None:
    """Persist auth options on the API object when supported."""
    if not isinstance(auth_options, dict):
        return

    auth_data = getattr(api, "_auth_data", None)
    if isinstance(auth_data, dict):
        auth_data.update(auth_options)
        return

    try:
        setattr(api, "_auth_data", dict(auth_options))
    except Exception:
        pass


def _fetch_auth_options(api: PyiCloudService) -> dict | None:
    """Fetch Apple auth options such as trusted phone numbers for HSA2."""
    endpoint = _auth_endpoint_for(api)
    if not endpoint:
        return None

    try:
        response = api.session.get(
            endpoint,
            headers=_auth_headers(api, {"Accept": "application/json"}),
        )
        if not response.ok:
            log.warning(
                "Apple-Auth-Optionen konnten nicht geladen werden (%s)",
                response.status_code,
            )
            return None
        data = response.json()
        if isinstance(data, dict):
            _cache_auth_options(api, data)
            return data
    except Exception as exc:
        log.warning("Apple-Auth-Optionen konnten nicht geladen werden: %s", exc)

    return None


def _trusted_phone_numbers(api: PyiCloudService) -> list[dict]:
    """Return trusted HSA2 phone numbers across pyicloud auth-data variants."""
    phones: list[dict] = []
    seen: set[str] = set()

    def _collect(source: dict | None) -> None:
        nonlocal phones, seen
        if not isinstance(source, dict):
            return

        candidates = []
        trusted_single = source.get("trustedPhoneNumber")
        if isinstance(trusted_single, dict):
            candidates.append(trusted_single)

        trusted_many = source.get("trustedPhoneNumbers")
        if isinstance(trusted_many, list):
            candidates.extend(trusted_many)

        for phone in candidates:
            if not isinstance(phone, dict):
                continue
            identifier = str(
                phone.get("id")
                or phone.get("numberWithDialCode")
                or phone.get("obfuscatedNumber")
                or phone.get("number")
                or ""
            )
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            phones.append(phone)

    _collect(getattr(api, "_auth_data", None))
    _collect(getattr(api, "data", None))

    if not phones:
        _collect(_fetch_auth_options(api))

    return phones


def _format_trusted_phones(phones: list[dict]) -> list[dict]:
    """Convert trusted phone metadata to the simplified API response shape."""
    result = []
    for index, phone in enumerate(phones):
        display = (
            phone.get("numberWithDialCode")
            or phone.get("obfuscatedNumber")
            or phone.get("number")
            or "Unbekannt"
        )
        result.append(
            {
                "index": index,
                "name": f"SMS an {display}",
                "phone": display,
            }
        )
    return result


def _is_invalid_verification_code(exc: Exception) -> bool:
    """Return True when Apple rejected the provided verification code."""
    if getattr(exc, "code", None) == -21669:
        return True
    msg = str(exc).lower()
    return "verification code" in msg or "security code" in msg


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
        msg = str(exc)
        if "No password" in msg or "password" in msg.lower():
            return {"status": "error", "message": "Session abgelaufen – bitte Passwort eingeben.", "requires_password": True}
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
            phones = _trusted_phone_numbers(api)
            if not phones:
                log.warning("Keine vertrauenswürdigen Telefonnummern für %s gefunden.", apple_id)
                return []
            _trusted_devices[apple_id] = phones
            return _format_trusted_phones(phones)
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


def _request_device_push(api: PyiCloudService) -> bool:
    """Ask Apple to send a 2FA push notification to trusted devices.

    Tries GET first (standard HSA2), then POST as fallback.
    Returns True if the request was accepted (2xx).
    """
    endpoint = _auth_endpoint_for(api)
    if not endpoint:
        log.warning("Apple-Auth-Endpoint nicht verfügbar; Push-Benachrichtigung nicht möglich.")
        return False

    url = f"{endpoint}/verify/trusteddevice"
    headers = _auth_headers(api, {"Accept": "application/json"})

    for method in (api.session.get, api.session.post):
        try:
            resp = method(url, headers=headers)
            log.info(
                "2FA push request %s %s → %s",
                method.__name__.upper(), url, resp.status_code,
            )
            if resp.ok:
                return True
        except Exception as exc:
            log.warning("2FA push %s fehlgeschlagen: %s", method.__name__.upper(), exc)

    return False


def request_2fa_push(apple_id: str, password: str | None = None) -> dict:
    """Trigger a 2FA push notification to all trusted Apple devices.

    If there is already an active session that requires 2FA, sends the
    push request directly.  Otherwise re-authenticates first.

    Returns a dict with:
        - success: bool
        - message: human-readable status message
        - status: auth status after the operation
    """
    api = _sessions.get(apple_id)

    # If no session or session doesn't need 2FA, re-authenticate
    if api is None or not api.requires_2fa:
        if not password:
            return {"success": False, "message": "Passwort erforderlich."}
        _sessions.pop(apple_id, None)
        result = authenticate(apple_id, password=password)
        if result["status"] != "requires_2fa":
            return {
                "success": result["status"] == "authenticated",
                "message": result["message"],
                "status": result["status"],
            }
        api = _sessions.get(apple_id)

    # Now we have a session that requires 2FA – request the push
    if api and _request_device_push(api):
        return {
            "success": True,
            "message": "Benachrichtigung an Ihre Apple-Geräte gesendet.",
            "status": "requires_2fa",
        }

    return {
        "success": False,
        "message": "Push-Benachrichtigung konnte nicht ausgelöst werden. Versuchen Sie SMS.",
        "status": "requires_2fa",
    }


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
        if not phone_id:
            return {"success": False, "message": "Telefonnummer-ID fehlt."}

        endpoint = _auth_endpoint_for(api)
        if not endpoint:
            return {"success": False, "message": "Apple-Auth-Endpoint nicht verfügbar."}

        push_mode = phone.get("pushMode") or phone.get("push_mode") or "sms"
        try:
            headers = _auth_headers(api, {"Accept": "application/json"})
            data = {"phoneNumber": {"id": phone_id}, "mode": push_mode}
            resp = api.session.put(
                f"{endpoint}/verify/phone",
                json=data,
                headers=headers,
            )
            if not resp.ok:
                return {"success": False, "message": f"Apple hat mit Status {resp.status_code} geantwortet."}

            try:
                resp_json = resp.json()
            except Exception:
                resp_json = None

            _cache_auth_options(api, resp_json)
            auth_data = getattr(api, "_auth_data", None)
            if isinstance(auth_data, dict):
                auth_data["mode"] = push_mode
                auth_data["trustedPhoneNumber"] = phone
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

    devices = _trusted_devices.get(apple_id, [])
    if device_index < 0 or device_index >= len(devices):
        return {"status": "error", "message": "Ungültiges Gerät."}

    # 2FA (HSA2): submit the SMS code via Apple's phone verification endpoint.
    if api.requires_2fa:
        phone = devices[device_index]
        phone_id = phone.get("id")
        if not phone_id:
            return {"status": "error", "message": "Telefonnummer-ID fehlt."}

        endpoint = _auth_endpoint_for(api)
        if not endpoint:
            return {"status": "error", "message": "Apple-Auth-Endpoint nicht verfügbar."}

        push_mode = phone.get("pushMode") or phone.get("push_mode") or "sms"
        try:
            api.session.post(
                f"{endpoint}/verify/phone/securitycode",
                json={
                    "phoneNumber": {"id": phone_id},
                    "securityCode": {"code": code},
                    "mode": push_mode,
                },
                headers=_auth_headers(api, {"Accept": "application/json"}),
            )
            api.trust_session()
        except Exception as exc:
            if _is_invalid_verification_code(exc):
                return {
                    "status": "error",
                    "message": "Ungültiger Code. Bitte versuchen Sie es erneut.",
                }
            return {"status": "error", "message": f"2FA-Fehler: {exc}"}

        return {
            "status": "authenticated",
            "message": "Zwei-Faktor-Authentifizierung erfolgreich.",
        }

    # 2SA (legacy): use traditional validation
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


def get_pending_2fa_session(apple_id: str) -> PyiCloudService | None:
    """Return the cached session that is waiting for a 2FA code, if any.

    While such a session exists, no new full login must be started for the
    account: every fresh SRP login makes Apple invalidate the previously
    sent verification code, so a competing login would break the code the
    user is about to enter.
    """
    api = _sessions.get(apple_id)
    if api is None:
        return None
    try:
        if api.requires_2fa or api.requires_2sa:
            return api
    except Exception:
        return None
    return None


def _stored_password_login(apple_id: str) -> PyiCloudService | None:
    """Attempt a fresh full login using the stored (opt-in) account password.

    Returns the resulting session – possibly in ``requires_2fa`` state, in
    which case it is also cached so a device push can be triggered on it –
    or None when no password is stored or the login failed.
    """
    pending = get_pending_2fa_session(apple_id)
    if pending is not None:
        return pending

    password = config_store.get_account_password(apple_id)
    if not password:
        return None

    try:
        api = PyiCloudService(
            apple_id=apple_id,
            password=password,
            cookie_directory=_cookie_dir_for(apple_id),
            verify=True,
        )
    except Exception as exc:
        log.warning(
            "Automatische Neuanmeldung mit gespeichertem Passwort für %s fehlgeschlagen: %s",
            apple_id,
            exc,
        )
        return None

    log.info(
        "Automatische Neuanmeldung mit gespeichertem Passwort für %s (requires_2fa=%s)",
        apple_id,
        api.requires_2fa or api.requires_2sa,
    )
    _sessions[apple_id] = api
    return api


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

    # Token reuse failed – if a password is stored, a fresh login may still
    # succeed without 2FA while the trust cookie is valid.
    api = _stored_password_login(apple_id)
    if api is not None and not api.requires_2fa and not api.requires_2sa:
        return api

    return None


def _fetch_cloudkit_owner(api) -> str | None:
    """Query the CloudKit Drive container for the current user's ``ownerRecordName``.

    Makes a lightweight ``/changes/database`` call against the
    ``com.apple.clouddocs`` container (the same technique that the Photos
    service uses for ``com.apple.photos.cloud``).  The response contains
    the user's zones, and each zone's ``zoneID`` includes the canonical
    ``ownerRecordName`` (format: ``_<32 hex chars>``).
    """
    try:
        ck_root = api.get_webservice_url("ckdatabasews")
    except Exception:
        log.debug("ckdatabasews URL nicht verfügbar")
        return None

    endpoint = f"{ck_root}/database/1/com.apple.clouddocs/production/private"
    url = f"{endpoint}/changes/database"
    try:
        resp = api.session.post(url, data="{}", headers={
            "Content-Type": "text/plain",
        })
        if not resp.ok:
            log.debug("CloudKit /changes/database Fehler: %s", resp.status_code)
            return None
        zones = resp.json().get("zones", [])
    except Exception as exc:
        log.debug("CloudKit /changes/database Aufruf fehlgeschlagen: %s", exc)
        return None

    # Look for the default zone or any non-deleted zone with an ownerRecordName
    for zone in zones:
        if zone.get("deleted"):
            continue
        owner = zone.get("zoneID", {}).get("ownerRecordName")
        if owner:
            log.info(
                "CloudKit ownerRecordName aus %s-Zone: %s",
                zone["zoneID"].get("zoneName", "?"),
                owner,
            )
            return owner

    log.debug("Keine Zone mit ownerRecordName in /changes/database Antwort")
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

    # Determine the current user's CloudKit ownerRecordName via the
    # ckdatabasews /changes/database endpoint — the only reliable source.
    user_record = _user_records.get(apple_id) or _fetch_cloudkit_owner(api)
    if user_record:
        _user_records[apple_id] = user_record
        log.info("CloudKit ownerRecordName für %s: %s", apple_id, user_record)
    else:
        log.warning(
            "Konnte CloudKit ownerRecordName für %s nicht ermitteln. "
            "Alle geteilten Ordner werden als Fremdfreigabe behandelt.",
            apple_id,
        )

    folders = []
    try:
        root = api.drive
        for child in root.dir():
            node = root[child]
            share_id = node.data.get("shareID")
            shared_not_owned = False
            if isinstance(share_id, dict):
                owner = share_id.get("zoneID", {}).get("ownerRecordName", "")
                if owner:
                    if user_record:
                        shared_not_owned = owner != user_record
                    else:
                        shared_not_owned = True
                    log.info(
                        "Ordner '%s': owner=%s → %s",
                        child,
                        owner,
                        "Fremdfreigabe" if shared_not_owned else "eigene Freigabe",
                    )
            folders.append(
                {
                    "name": child,
                    "type": "folder" if node.type == "folder" else "file",
                    "size": getattr(node, "size", None),
                    "shared_not_owned": shared_not_owned,
                }
            )
    except Exception as exc:
        log.error("Fehler beim Abrufen der Drive-Ordner für %s: %s", apple_id, exc)

    return sorted(folders, key=lambda f: (f["shared_not_owned"], f["name"].lower()))


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


def check_connection(apple_id: str) -> dict:
    """Check whether the iCloud session for *apple_id* is still valid.

    Attempts to reconnect using saved session tokens and performs a
    lightweight API call (listing Drive root) to verify the session
    actually works.

    Returns a dict with:
        - valid: bool – whether the session is usable
        - message: human-readable status
        - requires_2fa: bool – whether re-authentication with 2FA is needed
    """
    # A pending 2FA session must not be replaced by a fresh login – that
    # would invalidate the verification code the user is about to enter.
    if get_pending_2fa_session(apple_id) is not None:
        return {
            "valid": False,
            "message": "Zwei-Faktor-Authentifizierung ausstehend – bitte Code eingeben.",
            "requires_2fa": True,
        }

    # Drop cached session to force a fresh reconnect from saved tokens
    _sessions.pop(apple_id, None)

    cookie_dir = _cookie_dir_for(apple_id)
    try:
        api = PyiCloudService(
            apple_id=apple_id,
            cookie_directory=cookie_dir,
            verify=True,
        )
    except PyiCloudFailedLoginException as exc:
        # Saved token is dead – with a stored password, a fresh full login
        # can renew the session without asking the user for anything.
        api = _stored_password_login(apple_id)
        if api is None:
            msg = str(exc)
            if "No password" in msg or "password" in msg.lower():
                msg = "Session abgelaufen – bitte erneut anmelden."
            else:
                msg = f"Login fehlgeschlagen: {exc}"
            return {
                "valid": False,
                "message": msg,
                "requires_2fa": False,
                "requires_password": True,
            }
    except Exception as exc:
        return {
            "valid": False,
            "message": f"Verbindungsfehler: {exc}",
            "requires_2fa": False,
        }

    if api.requires_2fa or api.requires_2sa:
        _sessions[apple_id] = api
        return {
            "valid": False,
            "message": "Token abgelaufen – Zwei-Faktor-Authentifizierung erforderlich.",
            "requires_2fa": True,
        }

    # Verify the session with a lightweight API call
    try:
        api.drive.dir()
    except Exception as exc:
        log.warning("Verbindungscheck für %s: Drive-Zugriff fehlgeschlagen: %s", apple_id, exc)
        return {
            "valid": False,
            "message": f"Session ungültig – Drive-Zugriff fehlgeschlagen: {exc}",
            "requires_2fa": False,
        }

    _sessions[apple_id] = api
    return {
        "valid": True,
        "message": "Verbindung aktiv – Token ist gültig.",
        "requires_2fa": False,
    }


def get_storage_usage(apple_id: str) -> dict | None:
    """Fetch iCloud storage quota and per-media usage for *apple_id*.

    Returns a dict like::

        {
            "used_bytes": 123456789,
            "total_bytes": 5368709120,
            "available_bytes": 5245252331,
            "used_percent": 2.3,
            "quota_over": False,
            "media": [
                {"key": "photos", "label": "Fotos", "color": "#...", "usage_bytes": 100000},
                ...
            ]
        }

    Returns None when no session is available or the API call fails.
    """
    api = get_session(apple_id)
    if api is None:
        return None

    try:
        storage = api.account.storage
        usage = storage.usage

        _fallback_colors = [
            "#5EB0EF", "#F9C74F", "#F77F72", "#90BE6D",
            "#B497D6", "#F9844A", "#4ECDC4", "#AAB7B8",
        ]
        _hex_chars = set("0123456789abcdefABCDEF")

        def _css_color(raw) -> str | None:
            """Return a valid CSS hex colour or ``None``."""
            if not raw or not isinstance(raw, str):
                return None
            c = raw.strip().lstrip("#")
            if len(c) not in (3, 6, 8) or not _hex_chars.issuperset(c):
                return None
            return "#" + c

        media = []
        for idx, (_key, m) in enumerate((storage.usages_by_media or {}).items()):
            try:
                raw_color = m.color
            except (KeyError, AttributeError):
                raw_color = None
            media.append({
                "key": m.key,
                "label": m.label,
                "color": _css_color(raw_color) or _fallback_colors[idx % len(_fallback_colors)],
                "usage_bytes": m.usage_in_bytes,
            })

        # Sort by usage descending so the bar renders large segments first
        media.sort(key=lambda m: m["usage_bytes"], reverse=True)

        return {
            "used_bytes": usage.used_storage_in_bytes,
            "total_bytes": usage.total_storage_in_bytes,
            "available_bytes": usage.available_storage_in_bytes,
            "used_percent": usage.used_storage_in_percent,
            "quota_over": usage.quota_over,
            "media": media,
        }
    except Exception as exc:
        log.warning("iCloud-Speicherinfo für %s nicht abrufbar: %s", apple_id, exc)
        return None


def get_contacts(apple_id: str) -> list[dict] | None:
    """Fetch all contacts for the given account.

    Returns a list of contact dicts or None when no session is available.
    """
    api = get_session(apple_id)
    if api is None:
        return None

    try:
        return api.contacts.all
    except Exception as exc:
        log.error("Fehler beim Abrufen der Kontakte für %s: %s", apple_id, exc)
        return None


def get_calendars(apple_id: str) -> list[dict] | None:
    """Fetch all calendars for the given account.

    Returns a list of calendar dicts or None when no session is available.
    """
    api = get_session(apple_id)
    if api is None:
        return None

    try:
        return api.calendar.get_calendars()
    except Exception as exc:
        log.error("Fehler beim Abrufen der Kalender für %s: %s", apple_id, exc)
        return None


def get_calendar_events(
    apple_id: str,
    from_dt: "datetime | None" = None,
    to_dt: "datetime | None" = None,
) -> list[dict] | None:
    """Fetch calendar events for the given account and date range.

    Returns a list of event dicts or None when no session is available.
    """
    from datetime import datetime

    api = get_session(apple_id)
    if api is None:
        return None

    try:
        return api.calendar.get_events(from_dt=from_dt, to_dt=to_dt)
    except Exception as exc:
        log.error("Fehler beim Abrufen der Kalender-Events für %s: %s", apple_id, exc)
        return None


def _model_to_dict(obj):
    """Best-effort conversion of a pyicloud pydantic model / object to a dict."""
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    if isinstance(obj, dict):
        return obj
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


def _is_missing_zone_error(exc: Exception) -> bool:
    """Return True when the error means the account has no Notes/Reminders zone.

    Accounts that never used Notes or Reminders have no CloudKit zone, so Apple
    answers with ``ZONE_NOT_FOUND``. pyicloud surfaces this either directly in
    the message (Notes: ``Not Found (404): … ZONE_NOT_FOUND``) or wrapped as a
    response-validation error whose ``payload`` still carries the raw marker
    (Reminders: ``Changes response validation failed``). This is a benign
    "empty" condition, not a real backup failure.
    """
    markers = ("ZONE_NOT_FOUND", "Zone does not exist")
    haystack = str(exc)
    payload = getattr(exc, "payload", None)
    if payload is not None:
        try:
            haystack += json.dumps(payload, default=str)
        except Exception:
            haystack += repr(payload)
    return any(marker in haystack for marker in markers)


def get_reminders(apple_id: str) -> dict | None:
    """Fetch all reminder lists and reminders for the given account.

    Returns ``{"lists": [...], "reminders": [...]}`` (each a plain dict) or
    None when no session is available / the service is unreachable. An account
    without a Reminders zone yields empty lists rather than an error.
    """
    api = get_session(apple_id)
    if api is None:
        return None

    try:
        lists = [_model_to_dict(lst) for lst in api.reminders.lists()]
        reminders = [_model_to_dict(rem) for rem in api.reminders.reminders()]
        return {"lists": lists, "reminders": reminders}
    except Exception as exc:
        if _is_missing_zone_error(exc):
            log.info("Account %s hat keine Erinnerungen (Zone nicht vorhanden).", apple_id)
            return {"lists": [], "reminders": []}
        log.error("Fehler beim Abrufen der Erinnerungen für %s: %s", apple_id, exc)
        return None


def get_notes(apple_id: str) -> dict | None:
    """Fetch all Notes folders and notes (with text/html) for the account.

    Returns ``{"folders": [...], "notes": [...]}`` or None when no session is
    available. Locked notes are included as metadata but without content,
    since they cannot be decrypted server-side.
    """
    api = get_session(apple_id)
    if api is None:
        return None

    # pyicloud cached Anhang-Metadaten samt signierter Download-URLs im
    # Speicher. Die Signaturen laufen nach wenigen Stunden ab, daher liefert
    # eine wiederverwendete Session beim nächsten Lauf nur noch 410 Gone.
    # Cache vor jedem Abruf leeren, damit frische URLs geholt werden.
    att_cache = getattr(getattr(api, "notes", None), "_attachment_meta_cache", None)
    if isinstance(att_cache, dict):
        att_cache.clear()

    try:
        folders = [_model_to_dict(f) for f in api.notes.folders()]
        notes: list[dict] = []
        for summary in api.notes.iter_all():
            meta = _model_to_dict(summary)
            note_id = meta.get("id")
            if meta.get("is_deleted"):
                continue
            if not meta.get("is_locked") and note_id:
                try:
                    full = api.notes.get(note_id, with_attachments=True)
                    meta = _model_to_dict(full)
                except Exception as exc:
                    log.warning("Notiz %s konnte nicht geladen werden: %s", note_id, exc)
            notes.append(meta)
        return {"folders": folders, "notes": notes}
    except Exception as exc:
        if _is_missing_zone_error(exc):
            log.info("Account %s hat keine Notizen (Zone nicht vorhanden).", apple_id)
            return {"folders": [], "notes": []}
        log.error("Fehler beim Abrufen der Notizen für %s: %s", apple_id, exc)
        return None


def download_note_asset(apple_id: str, url: str, dest_path) -> bool:
    """Download a single Notes attachment URL to *dest_path*.

    Uses pyicloud's raw CloudKit asset downloader when available (it knows
    how to follow the signed asset URLs), falling back to a plain session
    GET. Returns True on success.
    """
    api = get_session(apple_id)
    if api is None or not url:
        return False

    raw = getattr(getattr(api, "notes", None), "_raw", None)
    try:
        if raw is not None and hasattr(raw, "download_asset_stream"):
            with open(dest_path, "wb") as fh:
                for chunk in raw.download_asset_stream(url):
                    fh.write(chunk)
            return True
        resp = api.session.get(url)
        if resp.ok:
            with open(dest_path, "wb") as fh:
                fh.write(resp.content)
            return True
        log.warning("Anhang-Download fehlgeschlagen (HTTP %s): %s", resp.status_code, url)
    except Exception as exc:
        log.warning("Anhang konnte nicht geladen werden (%s): %s", url, exc)
    return False


def disconnect(apple_id: str) -> None:
    """Remove a session from the in-memory cache."""
    _sessions.pop(apple_id, None)
