"""Backup scheduler using APScheduler – single central schedule."""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config_store
from app.services import backup_service
from app.services.notification import notify_backup_result, notify_token_expired, notify_token_expiring

# Token age (in days) at which a DSM warning is sent.
_TOKEN_WARNING_DAYS = 50

log = logging.getLogger("icloud-backup")

scheduler = AsyncIOScheduler()

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
            drive_folders=folders,
            photos_include_family=cfg.get("photos_include_family", False),
            shared_library_id=cfg.get("shared_library_id"),
            destination=cfg.get("destination", ""),
            exclusions=cfg.get("exclusions"),
            config_id=apple_id,
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
    """Check token age for a single account and send a DSM warning if expiring."""
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

    remaining = 60 - age_days
    if 0 < remaining <= (60 - _TOKEN_WARNING_DAYS):
        log.warning(
            "Token für %s ist %d Tage alt (noch ~%d Tage gültig)",
            apple_id, age_days, remaining,
        )
        notify_token_expiring(apple_id, remaining)


def _check_token_expiry() -> None:
    """Check token age for all accounts and send DSM warnings for expiring tokens."""
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
        )
        scheduler.add_job(
            _run_all_backups,
            trigger=trigger,
            id=_BACKUP_JOB_ID,
            replace_existing=True,
            name="Backup alle Accounts",
        )
        log.info("Zentraler Zeitplan registriert: %s", cron_expr)
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
