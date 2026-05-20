"""Backup scheduler using APScheduler – single central schedule."""

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config_store
from app.services import backup_service
from app.services.notification import notify_backup_result, notify_token_expired, notify_token_expiring

# Estimated iCloud session-token lifetime (in days) before re-auth is needed.
_TOKEN_LIFETIME_DAYS = 30
# Days of remaining validity at which a warning notification is sent.
_TOKEN_WARNING_REMAINING_DAYS = 7

log = logging.getLogger("icloud-backup")


def _resolve_timezone():
    """Resolve the scheduler timezone from the ``TZ`` env var.

    Falls back to UTC (with a warning) when ``TZ`` is unset or refers to a
    zone that is not installed in the container. This prevents APScheduler
    from silently scheduling jobs in UTC while the user assumes the cron
    expression is interpreted in their local timezone.
    """
    tz_name = os.getenv("TZ", "").strip()
    if not tz_name:
        log.warning("TZ-Umgebungsvariable nicht gesetzt – Zeitplan läuft in UTC.")
        return ZoneInfo("UTC")
    try:
        tz = ZoneInfo(tz_name)
        log.info("Zeitzone für Zeitplan: %s", tz_name)
        return tz
    except ZoneInfoNotFoundError:
        log.error(
            "TZ=%s konnte nicht aufgelöst werden (tzdata fehlt?). Falle auf UTC zurück.",
            tz_name,
        )
        return ZoneInfo("UTC")


scheduler = AsyncIOScheduler(
    job_defaults={"misfire_grace_time": 3600},
    timezone=_resolve_timezone(),
)

_BACKUP_JOB_ID = "backup_all"


def _parse_folders(cfg: dict) -> list[str]:
    """Extract the list of drive folders from a backup config dict."""
    if cfg.get("drive_config_mode", "simple") == "simple":
        return cfg.get("drive_folders_simple") or []
    else:
        # Advanced mode: one path per line
        text = cfg.get("drive_folders_advanced") or ""
        return [line.strip() for line in text.splitlines() if line.strip()]


async def _run_backup_job(apple_id: str) -> None:
    """Execute a single backup job for one account."""
    account = config_store.get_account(apple_id)
    if account is None:
        log.warning("Account %s nicht gefunden", apple_id)
        return
    if account["status"] != "authenticated":
        log.warning("Account %s nicht authentifiziert, überspringe Backup", apple_id)
        return

    cfg = config_store.get_backup_config(apple_id)
    if cfg is None:
        log.warning("Keine Backup-Konfiguration für %s", apple_id)
        return

    config_store.update_backup_status(
        apple_id,
        status="running",
        at=datetime.utcnow().isoformat(),
    )

    # Run the actual backup in a thread to avoid blocking the event loop
    try:
        folders = _parse_folders(cfg)
        result = await asyncio.to_thread(
            backup_service.run_backup,
            apple_id=apple_id,
            backup_drive=cfg.get("backup_drive", False),
            backup_photos=cfg.get("backup_photos", False),
            backup_contacts=cfg.get("backup_contacts", False),
            backup_calendar=cfg.get("backup_calendar", False),
            drive_folders=folders,
            photos_include_family=cfg.get("photos_include_family", False),
            shared_library_id=cfg.get("shared_library_id"),
            destination=cfg.get("destination", ""),
            exclusions=cfg.get("exclusions"),
            config_id=apple_id,
            contacts_sync_policy=cfg.get("contacts_sync_policy", "archive"),
            drive_sync_policy=cfg.get("drive_sync_policy", "delete"),
            photos_sync_policy=cfg.get("photos_sync_policy", "keep"),
        )

        status = "success" if result["success"] else "error"
        message = result["message"]
        dest = cfg.get("destination", "") or apple_id.replace("@", "_at_").replace(".", "_")
        storage = backup_service.get_backup_storage_stats(dest)
        stats = {
            "drive": result.get("drive_stats"),
            "photos": result.get("photos_stats"),
            "contacts": result.get("contacts_stats"),
            "calendar": result.get("calendar_stats"),
            "storage": storage,
        }
        if result.get("auth_expired"):
            config_store.update_account_status(
                apple_id, status="requires_2fa",
                status_message=message,
            )
            notify_token_expired(apple_id)
    except Exception as exc:
        log.error("Backup-Job für %s fehlgeschlagen: %s", apple_id, exc)
        status = "error"
        message = str(exc)
        stats = None

    config_store.update_backup_status(apple_id, status=status, message=message, stats=stats)
    notify_backup_result(apple_id, status, message)


def check_token_expiry_for_account(apple_id: str) -> None:
    """Check token age for a single account and send a warning notification if expiring."""
    acc = config_store.get_account(apple_id)
    if acc is None:
        return
    refresh_at = acc.get("last_token_refresh_at")
    if not refresh_at:
        return
    try:
        refresh_dt = datetime.fromisoformat(refresh_at)
        age_days = (datetime.now() - refresh_dt).days
    except (ValueError, TypeError):
        return

    remaining = _TOKEN_LIFETIME_DAYS - age_days
    if 0 < remaining <= _TOKEN_WARNING_REMAINING_DAYS:
        log.warning(
            "Token für %s ist %d Tage alt (noch ~%d Tage gültig)",
            apple_id, age_days, remaining,
        )
        notify_token_expiring(apple_id, remaining)


def _check_token_expiry() -> None:
    """Check token age for all accounts and send warning notifications for expiring tokens."""
    for acc in config_store.list_accounts():
        check_token_expiry_for_account(acc["apple_id"])


async def _run_all_backups() -> None:
    """Run backups for all configured accounts sequentially."""
    accounts = config_store.list_configured_accounts()
    if not accounts:
        log.info("Kein Account mit Backup-Konfiguration gefunden, überspringe geplanten Lauf")
        return

    # Check token expiry and send warnings before running backups
    _check_token_expiry()

    log.info("Geplanter Backup-Lauf gestartet für %d Account(s)", len(accounts))
    for acc in accounts:
        apple_id = acc["apple_id"]
        log.info("Starte Backup für %s", apple_id)
        await _run_backup_job(apple_id)
    log.info("Geplanter Backup-Lauf abgeschlossen")


async def sync_scheduled_jobs() -> None:
    """Read global schedule config and register/update the central backup job."""
    # Remove existing backup job
    existing = scheduler.get_job(_BACKUP_JOB_ID)
    if existing:
        existing.remove()

    schedule = config_store.get_schedule()
    if not schedule.get("enabled"):
        log.info("Zeitplan deaktiviert")
        return

    cron_expr = schedule.get("cron") or "0 2 * * *"
    try:
        parts = cron_expr.split()
        trigger = CronTrigger(
            minute=parts[0] if len(parts) > 0 else "0",
            hour=parts[1] if len(parts) > 1 else "2",
            day=parts[2] if len(parts) > 2 else "*",
            month=parts[3] if len(parts) > 3 else "*",
            day_of_week=parts[4] if len(parts) > 4 else "*",
            timezone=scheduler.timezone,
        )
        scheduler.add_job(
            _run_all_backups,
            trigger=trigger,
            id=_BACKUP_JOB_ID,
            replace_existing=True,
            name="Backup alle Accounts",
        )
        next_run = scheduler.get_job(_BACKUP_JOB_ID).next_run_time
        log.info(
            "Zentraler Zeitplan registriert: %s (TZ=%s, nächster Lauf: %s)",
            cron_expr, scheduler.timezone, next_run,
        )
    except Exception as exc:
        log.error("Ungültiger Cron-Ausdruck '%s': %s", cron_expr, exc)


def start_scheduler() -> None:
    """Start the APScheduler."""
    if not scheduler.running:
        scheduler.start()
        log.info("Scheduler gestartet")


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Scheduler gestoppt")
