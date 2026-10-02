"""Notification service – Pushover backend.

Settings are stored in the YAML config (``config_store.get_notifications``)
so they can be edited from the web UI instead of docker-compose.yml.
"""

import json
import logging
import urllib.error
import urllib.request

from app import config_store
from app.i18n import t

log = logging.getLogger("icloud-backup")

_PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


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
            t("notify.missing_credentials_log")
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
            log.info(t("notify.sent_log"), title)
    except urllib.error.HTTPError as exc:
        log.warning(
            t("notify.http_failed_log"),
            exc.code,
            exc.read().decode(errors="replace"),
        )
    except Exception as exc:
        log.warning(t("notify.failed_log"), exc)


def _send(title: str, message: str) -> None:
    send_pushover_notification(title, message)


def notify_backup_result(apple_id: str, status: str, message: str) -> None:
    """Send a notification summarising a backup result.

    Only sends for errors – successful backups are silent.
    """
    if status == "success":
        return

    _send(t("notify.backup_failed_title"), f"{apple_id}: {message}")


def notify_token_expired(apple_id: str) -> None:
    """Notify that an iCloud token has expired and 2FA is required."""
    _send(
        t("notify.token_expired_title"),
        t("notify.token_expired_body", apple_id=apple_id),
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
            "message": t("notify.missing_credentials"),
        }

    data = {
        "token": token,
        "user": user,
        "title": t("notify.test_title"),
        "message": t("notify.test_message"),
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
            log.info(t("notify.test_sent_log"))
            return {"success": True, "message": t("notify.test_sent")}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace").strip()
        return {
            "success": False,
            "message": t("notify.api_http_error", code=exc.code, body=body or t("notify.no_body")),
        }
    except Exception as exc:
        return {"success": False, "message": t("notify.failed", exc=exc)}
