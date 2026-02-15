"""DSM notification service using synodsmnotify."""

import logging
import shutil
import subprocess

from app.config import settings

log = logging.getLogger("icloud-backup")

_SYNODSMNOTIFY = "/usr/local/bin/synodsmnotify"


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
            "Bitte das Volume /usr/syno/bin/synodsmnotify:%s:ro in "
            "docker-compose.yml einbinden.",
            _SYNODSMNOTIFY,
            _SYNODSMNOTIFY,
        )
        return

    try:
        subprocess.run(
            [_SYNODSMNOTIFY, "@administrators", title, message],
            timeout=10,
            check=True,
            capture_output=True,
        )
        log.info("DSM-Benachrichtigung gesendet: %s", title)
    except subprocess.CalledProcessError as exc:
        log.warning("synodsmnotify fehlgeschlagen (rc=%d): %s", exc.returncode, exc.stderr.decode(errors="replace"))
    except FileNotFoundError:
        log.warning("synodsmnotify nicht gefunden")
    except Exception as exc:
        log.warning("DSM-Benachrichtigung fehlgeschlagen: %s", exc)


def notify_backup_result(apple_id: str, status: str, message: str) -> None:
    """Send a DSM notification summarising a backup result."""
    if status == "success":
        title = "iCloud Backup erfolgreich"
    else:
        title = "iCloud Backup fehlgeschlagen"

    body = f"{apple_id}: {message}"
    send_dsm_notification(title, body)
