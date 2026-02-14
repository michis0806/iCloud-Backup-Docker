"""API routes for backup configuration and execution."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException

from app import config_store
from app.schemas import BackupConfigCreate, BackupConfigResponse, BackupTriggerResponse
from app.services import backup_service, icloud_service
from app.services.scheduler import sync_scheduled_jobs

log = logging.getLogger("icloud-backup")
router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/configs/{apple_id}", response_model=BackupConfigResponse)
async def get_backup_config(apple_id: str):
    cfg = config_store.get_backup_config(apple_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")
    return cfg


@router.post("/configs/{apple_id}", response_model=BackupConfigResponse)
async def create_or_update_backup_config(apple_id: str, data: BackupConfigCreate):
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    cfg = config_store.save_backup_config(apple_id, data.model_dump())

    # Sync scheduler if schedule changed
    if data.schedule_enabled:
        await sync_scheduled_jobs()

    return cfg


@router.post("/run/{apple_id}", response_model=BackupTriggerResponse)
async def trigger_backup(apple_id: str):
    """Manually trigger a backup for the given account."""
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")
    if account["status"] != "authenticated":
        raise HTTPException(status_code=400, detail="Account nicht authentifiziert.")

    cfg = config_store.get_backup_config(apple_id)
    if cfg is None or (not cfg.get("backup_drive") and not cfg.get("backup_photos")):
        raise HTTPException(status_code=400, detail="Keine Backup-Konfiguration vorhanden.")

    # Mark as running
    config_store.update_backup_status(
        apple_id,
        status="running",
        at=datetime.utcnow().isoformat(),
    )

    # Parse folders
    if cfg.get("drive_config_mode", "simple") == "simple":
        folders = cfg.get("drive_folders_simple") or []
    else:
        text = cfg.get("drive_folders_advanced") or ""
        folders = [line.strip() for line in text.splitlines() if line.strip()]

    # Run backup in background thread
    async def _run():
        try:
            result = await asyncio.to_thread(
                backup_service.run_backup,
                apple_id=apple_id,
                backup_drive=cfg.get("backup_drive", False),
                backup_photos=cfg.get("backup_photos", False),
                drive_folders=folders,
                photos_include_family=cfg.get("photos_include_family", False),
                destination=cfg.get("destination", ""),
                exclusions=cfg.get("exclusions"),
                config_id=apple_id,
            )
            status = "success" if result["success"] else "error"
            message = result["message"]
            stats = {
                "drive": result.get("drive_stats"),
                "photos": result.get("photos_stats"),
            }
        except Exception as exc:
            log.error("Backup fehlgeschlagen für %s: %s", apple_id, exc)
            status = "error"
            message = str(exc)
            stats = None

        config_store.update_backup_status(apple_id, status=status, message=message, stats=stats)

    asyncio.create_task(_run())

    return BackupTriggerResponse(
        message="Backup gestartet.",
        apple_id=apple_id,
    )


@router.get("/status/{apple_id}")
async def get_backup_status(apple_id: str):
    """Get current backup status for an account."""
    cfg = config_store.get_backup_config(apple_id)
    if cfg is None:
        return {
            "status": "not_configured",
            "message": "Keine Backup-Konfiguration vorhanden.",
        }

    return {
        "status": cfg.get("last_backup_status", "idle"),
        "message": cfg.get("last_backup_message"),
        "last_backup_at": cfg.get("last_backup_at"),
        "stats": cfg.get("last_backup_stats"),
    }
