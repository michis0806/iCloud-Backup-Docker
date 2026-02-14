"""Backup scheduler using APScheduler."""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.database import async_session
from app.models import BackupConfig, BackupStatus, Account, AccountStatus
from app.services import backup_service

log = logging.getLogger("icloud-backup")

scheduler = AsyncIOScheduler()


def _parse_folders(config: BackupConfig) -> list[str]:
    """Extract the list of drive folders from a backup config."""
    if config.drive_config_mode.value == "simple":
        return config.drive_folders_simple or []
    else:
        # Advanced mode: one path per line
        text = config.drive_folders_advanced or ""
        return [line.strip() for line in text.splitlines() if line.strip()]


async def _run_backup_job(config_id: int) -> None:
    """Execute a single backup job."""
    async with async_session() as session:
        config = await session.get(BackupConfig, config_id)
        if config is None:
            log.warning("Backup-Konfiguration %d nicht gefunden", config_id)
            return

        account = await session.get(Account, config.account_id)
        if account is None or account.status != AccountStatus.AUTHENTICATED:
            log.warning("Account %d nicht authentifiziert, überspringe Backup", config.account_id)
            return

        config.last_backup_status = BackupStatus.RUNNING
        config.last_backup_at = datetime.utcnow()
        await session.commit()

    # Run the actual backup in a thread to avoid blocking the event loop
    try:
        folders = _parse_folders(config)
        result = await asyncio.to_thread(
            backup_service.run_backup,
            apple_id=account.apple_id,
            backup_drive=config.backup_drive,
            backup_photos=config.backup_photos,
            drive_folders=folders,
            photos_include_family=config.photos_include_family,
            destination=config.destination,
            exclusions=config.exclusions,
        )

        status = BackupStatus.SUCCESS if result["success"] else BackupStatus.ERROR
        message = result["message"]
        stats = {
            "drive": result.get("drive_stats"),
            "photos": result.get("photos_stats"),
        }
    except Exception as exc:
        log.error("Backup-Job %d fehlgeschlagen: %s", config_id, exc)
        status = BackupStatus.ERROR
        message = str(exc)
        stats = None

    async with async_session() as session:
        config = await session.get(BackupConfig, config_id)
        if config:
            config.last_backup_status = status
            config.last_backup_message = message
            config.last_backup_stats = stats
            await session.commit()


async def sync_scheduled_jobs() -> None:
    """Read all backup configs from DB and register/update scheduled jobs."""
    async with async_session() as session:
        result = await session.execute(
            select(BackupConfig).where(BackupConfig.schedule_enabled.is_(True))
        )
        configs = result.scalars().all()

    # Remove all existing jobs
    for job in scheduler.get_jobs():
        if job.id.startswith("backup_"):
            job.remove()

    # Add jobs for enabled schedules
    for config in configs:
        cron_expr = config.schedule_cron or "0 2 * * *"  # Default: 2 AM daily
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
                _run_backup_job,
                trigger=trigger,
                id=f"backup_{config.id}",
                args=[config.id],
                replace_existing=True,
                name=f"Backup Config #{config.id}",
            )
            log.info("Geplanter Job registriert: Config #%d (%s)", config.id, cron_expr)
        except Exception as exc:
            log.error(
                "Ungültiger Cron-Ausdruck '%s' für Config #%d: %s",
                cron_expr, config.id, exc,
            )


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
