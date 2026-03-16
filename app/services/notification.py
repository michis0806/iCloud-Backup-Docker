"""Notification service – supports DSM (synodsmnotify) and Pushover."""

import json
import logging
import os
import shutil
import subprocess
import urllib.request
import urllib.error

from app.config import settings

log = logging.getLogger("icloud-backup")

# ---------------------------------------------------------------------------
# DSM (Synology) backend
# ---------------------------------------------------------------------------

_SYNODSMNOTIFY = "/usr/local/bin/synodsmnotify"
_SYNO_LIB_DIR = "/usr/syno/lib"


def _binary_available() -> bool:
    """Check whether synodsmnotify is available in the container."""
    return shutil.which(_SYNODSMNOTIFY) is not None


def send_dsm_notification(title: str, message: str) -> None:
    """Send a DSM notification via synodsmnotify.

    Does nothing when DSM_NOTIFY is disabled or the binary is missing.
    """
    if not settings.dsm_notify:
        return

    if not _binary_available():
        log.warning(
            "DSM_NOTIFY ist aktiviert, aber %s wurde nicht gefunden. "
            "Bitte die Volumes /usr/syno/bin/synodsmnotify:%s:ro und "
            "/usr/lib:%s:ro in docker-compose.yml einbinden.",
            _SYNODSMNOTIFY,
            _SYNODSMNOTIFY,
            _SYNO_LIB_DIR,
        )
        return

    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = _SYNO_LIB_DIR + ":" + env.get("LD_LIBRARY_PATH", "")

    try:
        subprocess.run(
            [_SYNODSMNOTIFY, "@administrators", title, message],
            timeout=10,
            check=True,
            capture_output=True,
            env=env,
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

    Does nothing when PUSHOVER_ENABLED is false or credentials are missing.
    """
    if not settings.pushover_enabled:
        return

    if not settings.pushover_api_token or not settings.pushover_user_key:
        log.warning(
            "PUSHOVER_ENABLED ist aktiviert, aber PUSHOVER_API_TOKEN oder "
            "PUSHOVER_USER_KEY fehlt."
        )
        return

    payload = json.dumps({
        "token": settings.pushover_api_token,
        "user": settings.pushover_user_key,
        "title": title,
        "message": message,
    }).encode()

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
