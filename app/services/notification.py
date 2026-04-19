"""Notification service – supports DSM (synodsmnotify) and Pushover.

Settings are stored in the YAML config (``config_store.get_notifications``)
so they can be edited from the web UI instead of docker-compose.yml.
"""

import json
import logging
import os
import shutil
import subprocess
import urllib.request
import urllib.error

from app import config_store

log = logging.getLogger("icloud-backup")

# ---------------------------------------------------------------------------
# DSM (Synology) backend
# ---------------------------------------------------------------------------

_SYNODSMNOTIFY = "/usr/local/bin/synodsmnotify"
_SYNO_LIB_DIR = "/usr/syno/lib"

# DSM 7.x requires the positional "title" arg to be a registered mail string
# key and the "msg" arg to be a JSON object whose keys map to the placeholders
# defined in that mail template. We use the built-in ``DSMSupportFormCustomMessage``
# template, which exposes a single free-form placeholder ``%CUSTOM_MSG%`` and
# has no hardcoded title/subject, so our own text is rendered verbatim.
_DSM_MAIL_KEY = "DSMSupportFormCustomMessage"


def _binary_available() -> bool:
    """Check whether synodsmnotify is available in the container."""
    return shutil.which(_SYNODSMNOTIFY) is not None


def _dsm_env() -> dict:
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = _SYNO_LIB_DIR + ":" + env.get("LD_LIBRARY_PATH", "")
    return env


def _dsm_payload(title: str, message: str) -> str:
    """Build the JSON string for the synodsmnotify ``msg`` positional arg."""
    text = f"{title}: {message}" if title and message else (title or message)
    return json.dumps({"CUSTOM_MSG": text})


def send_dsm_notification(title: str, message: str) -> None:
    """Send a DSM notification via synodsmnotify.

    Does nothing when DSM notifications are disabled or the binary is missing.
    """
    if not config_store.get_notifications().get("dsm_notify"):
        return

    if not _binary_available():
        log.warning(
            "DSM-Benachrichtigungen sind aktiviert, aber %s wurde nicht gefunden. "
            "Bitte die Volumes /usr/syno/bin/synodsmnotify:%s:ro und "
            "/usr/lib:%s:ro in docker-compose.yml einbinden.",
            _SYNODSMNOTIFY,
            _SYNODSMNOTIFY,
            _SYNO_LIB_DIR,
        )
        return

    try:
        subprocess.run(
            [_SYNODSMNOTIFY, "@administrators", _DSM_MAIL_KEY, _dsm_payload(title, message)],
            timeout=10,
            check=True,
            capture_output=True,
            env=_dsm_env(),
        )
        log.info("DSM-Benachrichtigung gesendet: %s", title)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace")
        if exc.returncode == 127 and "shared librar" in stderr:
            log.warning(
                "synodsmnotify fehlgeschlagen (rc=127): Shared Libraries fehlen. "
                "Bitte /usr/lib:%s:ro als Volume einbinden. Detail: %s",
                _SYNO_LIB_DIR,
                stderr,
            )
        else:
            log.warning("synodsmnotify fehlgeschlagen (rc=%d): %s", exc.returncode, stderr)
    except FileNotFoundError:
        log.warning("synodsmnotify nicht gefunden")
    except Exception as exc:
        log.warning("DSM-Benachrichtigung fehlgeschlagen: %s", exc)


# ---------------------------------------------------------------------------
# Pushover backend
# ---------------------------------------------------------------------------

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

    payload = json.dumps(data).encode()

    req = urllib.request.Request(
        _PUSHOVER_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10):
            log.info("Pushover-Benachrichtigung gesendet: %s", title)
    except urllib.error.HTTPError as exc:
        log.warning("Pushover-Benachrichtigung fehlgeschlagen (HTTP %d): %s", exc.code, exc.read().decode(errors="replace"))
    except Exception as exc:
        log.warning("Pushover-Benachrichtigung fehlgeschlagen: %s", exc)


# ---------------------------------------------------------------------------
# Unified helpers – dispatch to all enabled backends
# ---------------------------------------------------------------------------

def _send(title: str, message: str) -> None:
    """Send a notification to all enabled backends."""
    send_dsm_notification(title, message)
    send_pushover_notification(title, message)


def notify_backup_result(apple_id: str, status: str, message: str) -> None:
    """Send a notification summarising a backup result.

    Only sends for errors – successful backups are silent.
    """
    if status == "success":
        return

    _send("iCloud Backup fehlgeschlagen", f"{apple_id}: {message}")


def notify_token_expiring(apple_id: str, days_remaining: int) -> None:
    """Warn that an iCloud token is about to expire."""
    _send(
        "iCloud Token läuft bald ab",
        f"{apple_id}: Token läuft in ca. {days_remaining} Tagen ab. "
        "Bitte erneuern Sie die Verbindung.",
    )


def notify_token_expired(apple_id: str) -> None:
    """Notify that an iCloud token has expired and 2FA is required."""
    _send(
        "iCloud Token abgelaufen",
        f"{apple_id}: Token ist abgelaufen. "
        "Zwei-Faktor-Authentifizierung erforderlich.",
    )


# ---------------------------------------------------------------------------
# Test helpers – bypass the "enabled" toggle and return structured results
# so the UI can display a success/failure message.
# ---------------------------------------------------------------------------

_TEST_TITLE = "iCloud Backup – Testbenachrichtigung"
_TEST_MESSAGE = "Dies ist eine Testnachricht aus dem iCloud-Backup-Service."


def test_dsm() -> dict:
    """Send a one-off DSM notification and report the result."""
    if not _binary_available():
        return {
            "success": False,
            "message": (
                f"{_SYNODSMNOTIFY} wurde nicht gefunden. Bitte die Volumes "
                f"/usr/syno/bin/synodsmnotify:{_SYNODSMNOTIFY}:ro und "
                f"/usr/lib:{_SYNO_LIB_DIR}:ro in docker-compose.yml einbinden."
            ),
        }

    try:
        subprocess.run(
            [_SYNODSMNOTIFY, "@administrators", _DSM_MAIL_KEY, _dsm_payload(_TEST_TITLE, _TEST_MESSAGE)],
            timeout=10,
            check=True,
            capture_output=True,
            env=_dsm_env(),
        )
        log.info("DSM-Testbenachrichtigung gesendet")
        return {"success": True, "message": "DSM-Testbenachrichtigung gesendet."}
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        return {
            "success": False,
            "message": f"synodsmnotify fehlgeschlagen (rc={exc.returncode}): {stderr or '(keine Ausgabe)'}",
        }
    except FileNotFoundError:
        return {"success": False, "message": "synodsmnotify nicht gefunden."}
    except Exception as exc:
        return {"success": False, "message": f"DSM-Benachrichtigung fehlgeschlagen: {exc}"}


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
