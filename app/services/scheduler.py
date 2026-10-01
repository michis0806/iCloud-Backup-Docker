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
from app.services.notification import notify_backup_result, notify_token_expired
from app.i18n import t

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
        log.warning(t("sched.tz_unset"))
        return ZoneInfo("UTC")
    try:
        tz = ZoneInfo(tz_name)
        log.info(t("sched.tz"), tz_name)
        return tz
    except ZoneInfoNotFoundError:
        log.error(
            t("sched.tz_unresolved"),
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
        log.warning(t("sched.account_not_found"), apple_id)
        return
    if account["status"] != "authenticated":
        log.warning(t("sched.not_authenticated"), apple_id)
        return

    cfg = config_store.get_backup_config(apple_id)
    if cfg is None:
        log.warning(t("sched.no_config"), apple_id)
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
            backup_notes=cfg.get("backup_notes", False),
            backup_reminders=cfg.get("backup_reminders", False),
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

        if result.get("skipped_already_running"):
            log.info(t("sched.skipped_running"), apple_id)
            return
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
        log.error(t("sched.job_failed"), apple_id, exc)
        status = "error"
        message = str(exc)
        stats = None

    config_store.update_backup_status(apple_id, status=status, message=message, stats=stats)
    notify_backup_result(apple_id, status, message)


async def _run_all_backups() -> None:
    """Run backups for all configured accounts sequentially."""
    accounts = config_store.list_configured_accounts()
    if not accounts:
        log.info(t("sched.no_accounts"))
        return

    log.info(t("sched.run_started"), len(accounts))
    for acc in accounts:
        apple_id = acc["apple_id"]
        log.info(t("sched.starting"), apple_id)
        await _run_backup_job(apple_id)
    log.info(t("sched.run_done"))


async def sync_scheduled_jobs() -> None:
    """Read global schedule config and register/update the central backup job."""
    # Remove existing backup job
    existing = scheduler.get_job(_BACKUP_JOB_ID)
    if existing:
        existing.remove()

    schedule = config_store.get_schedule()
    if not schedule.get("enabled"):
        log.info(t("sched.disabled"))
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
            name=t("sched.job_name"),
        )
        next_run = scheduler.get_job(_BACKUP_JOB_ID).next_run_time
        log.info(
            t("sched.registered"),
            cron_expr, scheduler.timezone, next_run,
        )
    except Exception as exc:
        log.error(t("sched.bad_cron"), cron_expr, exc)


def start_scheduler() -> None:
    """Start the APScheduler."""
    if not scheduler.running:
        scheduler.start()
        log.info(t("sched.started"))


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info(t("sched.stopped"))
