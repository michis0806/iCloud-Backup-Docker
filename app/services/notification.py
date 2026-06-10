"""Notification service – Pushover backend.

Settings are stored in the YAML config (``config_store.get_notifications``)
so they can be edited from the web UI instead of docker-compose.yml.
"""

import json
import logging
import urllib.error
import urllib.request

from app import config_store

log = logging.getLogger("icloud-backup")

_PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

_TEST_TITLE = "iCloud Backup – Testbenachrichtigung"
_TEST_MESSAGE = "Dies ist eine Testnachricht aus dem iCloud-Backup-Service."


def send_pushover_notification(title: str, message: str) -> None:
    """Send a push notification via the Pushover API.

    Does nothing when Pushover is disabled or credentials are missing.
    """
    notif = config_store.get_notifications()
    if not notif.get("pushover_enabled"):
        return

    token = notif.get("pushover_api_token") or ""
    user = notif.get("pushover_user_key") or ""
    devices = notif.get("pushover_devices") or ""
    if not token or not user:
        log.warning(
            "Pushover ist aktiviert, aber API-Token oder User-Key fehlt. "
            "Bitte in den Benachrichtigungseinstellungen ergänzen."
        )
        return

    data = {
        "token": token,
        "user": user,
        "title": title,
        "message": message,
    }
    if devices:
        data["device"] = devices

    req = urllib.request.Request(
        _PUSHOVER_API_URL,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10):
            log.info("Pushover-Benachrichtigung gesendet: %s", title)
    except urllib.error.HTTPError as exc:
        log.warning(
            "Pushover-Benachrichtigung fehlgeschlagen (HTTP %d): %s",
            exc.code,
            exc.read().decode(errors="replace"),
        )
    except Exception as exc:
        log.warning("Pushover-Benachrichtigung fehlgeschlagen: %s", exc)


def _send(title: str, message: str) -> None:
    send_pushover_notification(title, message)


def notify_backup_result(apple_id: str, status: str, message: str) -> None:
    """Send a notification summarising a backup result.

    Only sends for errors – successful backups are silent.
    """
    if status == "success":
        return

    _send("iCloud Backup fehlgeschlagen", f"{apple_id}: {message}")


def notify_token_expired(apple_id: str) -> None:
    """Notify that an iCloud token has expired and 2FA is required."""
    _send(
        "iCloud Token abgelaufen",
        f"{apple_id}: Token ist abgelaufen. "
        "Zwei-Faktor-Authentifizierung erforderlich.",
    )


def test_pushover() -> dict:
    """Send a one-off Pushover notification and report the result."""
    notif = config_store.get_notifications()
    token = (notif.get("pushover_api_token") or "").strip()
    user = (notif.get("pushover_user_key") or "").strip()
    devices = (notif.get("pushover_devices") or "").strip()

    if not token or not user:
        return {
            "success": False,
            "message": "API-Token oder User-Key fehlt. Bitte zuerst speichern.",
        }

    data = {
        "token": token,
        "user": user,
        "title": _TEST_TITLE,
        "message": _TEST_MESSAGE,
    }
    if devices:
        data["device"] = devices

    req = urllib.request.Request(
        _PUSHOVER_API_URL,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10):
            log.info("Pushover-Testbenachrichtigung gesendet")
            return {"success": True, "message": "Pushover-Testbenachrichtigung gesendet."}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace").strip()
        return {
            "success": False,
            "message": f"Pushover-API antwortete mit HTTP {exc.code}: {body or '(kein Body)'}",
        }
    except Exception as exc:
        return {"success": False, "message": f"Pushover-Benachrichtigung fehlgeschlagen: {exc}"}
