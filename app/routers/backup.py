"""API routes for backup configuration and execution."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    Account,
    AccountStatus,
    BackupConfig,
    BackupStatus,
    DriveConfigMode,
)
from app.schemas import BackupConfigCreate, BackupConfigResponse, BackupTriggerResponse
from app.services import backup_service, icloud_service
from app.services.scheduler import sync_scheduled_jobs

log = logging.getLogger("icloud-backup")
router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/configs/{account_id}", response_model=list[BackupConfigResponse])
async def get_backup_configs(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BackupConfig).where(BackupConfig.account_id == account_id)
    )
    return result.scalars().all()


@router.post("/configs/{account_id}", response_model=BackupConfigResponse)
async def create_or_update_backup_config(
    account_id: int,
    data: BackupConfigCreate,
    db: AsyncSession = Depends(get_db),
):
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    # Check if a config already exists for this account
    result = await db.execute(
        select(BackupConfig).where(BackupConfig.account_id == account_id)
    )
    config = result.scalar_one_or_none()

    if config is None:
        config = BackupConfig(account_id=account_id)
        db.add(config)

    # Update fields
    config.backup_drive = data.backup_drive
    config.backup_photos = data.backup_photos
    config.drive_config_mode = data.drive_config_mode
    config.drive_folders_simple = data.drive_folders_simple
    config.drive_folders_advanced = data.drive_folders_advanced
    config.photos_include_family = data.photos_include_family
    config.exclusions = data.exclusions
    config.schedule_enabled = data.schedule_enabled
    config.schedule_cron = data.schedule_cron
    config.updated_at = datetime.utcnow()

    # Auto-generate destination if not set
    if not config.destination:
        config.destination = account.apple_id.replace("@", "_at_").replace(".", "_")

    if data.destination:
        config.destination = data.destination

    await db.commit()
    await db.refresh(config)

    # Sync scheduler if schedule changed
    if data.schedule_enabled:
        await sync_scheduled_jobs()

    return config


@router.post("/run/{account_id}", response_model=BackupTriggerResponse)
async def trigger_backup(account_id: int, db: AsyncSession = Depends(get_db)):
    """Manually trigger a backup for the given account."""
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")
    if account.status != AccountStatus.AUTHENTICATED:
        raise HTTPException(status_code=400, detail="Account nicht authentifiziert.")

    result = await db.execute(
        select(BackupConfig).where(BackupConfig.account_id == account_id)
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=400, detail="Keine Backup-Konfiguration vorhanden.")

    # Mark as running
    config.last_backup_status = BackupStatus.RUNNING
    config.last_backup_at = datetime.utcnow()
    await db.commit()

    # Parse folders
    if config.drive_config_mode == DriveConfigMode.SIMPLE:
        folders = config.drive_folders_simple or []
    else:
        text = config.drive_folders_advanced or ""
        folders = [line.strip() for line in text.splitlines() if line.strip()]

    # Run backup in background thread
    async def _run():
        try:
            result = await asyncio.to_thread(
                backup_service.run_backup,
                apple_id=account.apple_id,
                backup_drive=config.backup_drive,
                backup_photos=config.backup_photos,
                drive_folders=folders,
                photos_include_family=config.photos_include_family,
                destination=config.destination,
                exclusions=config.exclusions,
                config_id=config.id,
            )
            status = BackupStatus.SUCCESS if result["success"] else BackupStatus.ERROR
            message = result["message"]
            stats = {
                "drive": result.get("drive_stats"),
                "photos": result.get("photos_stats"),
            }
        except Exception as exc:
            log.error("Backup fehlgeschlagen für %s: %s", account.apple_id, exc)
            status = BackupStatus.ERROR
            message = str(exc)
            stats = None

        async with db.begin():
            config_obj = await db.get(BackupConfig, config.id)
            if config_obj:
                config_obj.last_backup_status = status
                config_obj.last_backup_message = message
                config_obj.last_backup_stats = stats

    asyncio.create_task(_run())

    return BackupTriggerResponse(
        message="Backup gestartet.",
        account_id=account_id,
    )


@router.get("/status/{account_id}")
async def get_backup_status(account_id: int, db: AsyncSession = Depends(get_db)):
    """Get current backup status for an account."""
    result = await db.execute(
        select(BackupConfig).where(BackupConfig.account_id == account_id)
    )
    config = result.scalar_one_or_none()
    if config is None:
        return {
            "status": "not_configured",
            "message": "Keine Backup-Konfiguration vorhanden.",
        }

    return {
        "status": config.last_backup_status.value,
        "message": config.last_backup_message,
        "last_backup_at": config.last_backup_at.isoformat() if config.last_backup_at else None,
        "stats": config.last_backup_stats,
    }
