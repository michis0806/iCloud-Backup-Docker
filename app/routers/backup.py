"""API routes for backup configuration and execution."""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app import config_store
from app.schemas import (
    BackupConfigCreate, BackupConfigResponse, BackupTriggerResponse,
    ScheduleUpdate, ScheduleResponse,
)
from app.services import backup_service, icloud_service
from app.services.notification import notify_backup_result, notify_token_expired
from app.services.scheduler import check_token_expiry_for_account, sync_scheduled_jobs

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
    start_time = datetime.now(timezone.utc)
    config_store.update_backup_status(
        apple_id,
        status="running",
        started_at=start_time.isoformat(),
    )

    # Parse folders
    if cfg.get("drive_config_mode", "simple") == "simple":
        folders = cfg.get("drive_folders_simple") or []
    else:
        text = cfg.get("drive_folders_advanced") or ""
        folders = [line.strip() for line in text.splitlines() if line.strip()]

    # Check token expiry before starting
    check_token_expiry_for_account(apple_id)

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
                shared_library_id=cfg.get("shared_library_id"),
                destination=cfg.get("destination", ""),
                exclusions=cfg.get("exclusions"),
                config_id=apple_id,
                drive_sync_policy=cfg.get("drive_sync_policy", "delete"),
                photos_sync_policy=cfg.get("photos_sync_policy", "keep"),
            )
            status = "success" if result["success"] else "error"
            message = result["message"]
            # Scan local backup dirs for file counts and sizes
            dest = cfg.get("destination", "") or apple_id.replace("@", "_at_").replace(".", "_")
            storage = backup_service.get_backup_storage_stats(dest)
            stats = {
                "drive": result.get("drive_stats"),
                "photos": result.get("photos_stats"),
                "storage": storage,
            }
            # Notify and update account status when token has expired
            if result.get("auth_expired"):
                config_store.update_account_status(
                    apple_id, status="requires_2fa",
                    status_message=message,
                )
                notify_token_expired(apple_id)
        except Exception as exc:
            log.error("Backup fehlgeschlagen für %s: %s", apple_id, exc)
            status = "error"
            message = str(exc)
            stats = None

        end_time = datetime.now(timezone.utc)
        duration = round((end_time - start_time).total_seconds())
        config_store.update_backup_status(
            apple_id, status=status, message=message, stats=stats,
            at=end_time.isoformat(), duration_seconds=duration,
        )
        notify_backup_result(apple_id, status, message)

    asyncio.create_task(_run())

    return BackupTriggerResponse(
        message="Backup gestartet.",
        apple_id=apple_id,
    )


@router.post("/run-all")
async def trigger_all_backups():
    """Manually trigger backups for all configured and authenticated accounts."""
    accounts = config_store.list_configured_accounts()
    triggered = []
    for acc in accounts:
        apple_id = acc["apple_id"]
        account = config_store.get_account(apple_id)
        if account is None or account["status"] != "authenticated":
            continue
        cfg = config_store.get_backup_config(apple_id)
        if cfg is None or (not cfg.get("backup_drive") and not cfg.get("backup_photos")):
            continue

        # Check if already running
        if backup_service.get_progress(apple_id) is not None:
            continue

        check_token_expiry_for_account(apple_id)

        run_start_time = datetime.now(timezone.utc)
        config_store.update_backup_status(
            apple_id, status="running", started_at=run_start_time.isoformat(),
        )

        if cfg.get("drive_config_mode", "simple") == "simple":
            folders = cfg.get("drive_folders_simple") or []
        else:
            text = cfg.get("drive_folders_advanced") or ""
            folders = [line.strip() for line in text.splitlines() if line.strip()]

        async def _run(apple_id=apple_id, cfg=cfg, folders=folders, _start=run_start_time):
            try:
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
                log.error("Backup fehlgeschlagen für %s: %s", apple_id, exc)
                status = "error"
                message = str(exc)
                stats = None

            end_time = datetime.now(timezone.utc)
            duration = round((end_time - _start).total_seconds())
            config_store.update_backup_status(
                apple_id, status=status, message=message, stats=stats,
                at=end_time.isoformat(), duration_seconds=duration,
            )
            notify_backup_result(apple_id, status, message)

        asyncio.create_task(_run())
        triggered.append(apple_id)

    if not triggered:
        raise HTTPException(status_code=400, detail="Keine konfigurierten Accounts gefunden.")
    return {"message": f"Backup gestartet für {len(triggered)} Account(s).", "triggered": triggered}


@router.post("/cancel/{apple_id}")
async def cancel_backup(apple_id: str):
    """Cancel a running backup for the given account."""
    cancelled = backup_service.request_cancel(apple_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Kein laufendes Backup gefunden.")
    return {"message": "Abbruch angefordert.", "apple_id": apple_id}


@router.get("/status/{apple_id}")
async def get_backup_status(apple_id: str):
    """Get current backup status for an account."""
    cfg = config_store.get_backup_config(apple_id)
    if cfg is None:
        return {
            "status": "not_configured",
            "message": "Keine Backup-Konfiguration vorhanden.",
        }

    status = cfg.get("last_backup_status", "idle")

    # Guard against phantom "running" state: if persisted status says running
    # but no backup process is actually active, correct it.
    if status == "running" and backup_service.get_progress(apple_id) is None:
        status = "error"
        config_store.update_backup_status(
            apple_id, status="error",
            message="Backup durch Neustart unterbrochen.",
        )

    return {
        "status": status,
        "message": cfg.get("last_backup_message"),
        "last_backup_at": cfg.get("last_backup_at"),
        "last_backup_started_at": cfg.get("last_backup_started_at"),
        "last_backup_duration_seconds": cfg.get("last_backup_duration_seconds"),
        "stats": cfg.get("last_backup_stats"),
    }


# ---------------------------------------------------------------------------
# Global schedule
# ---------------------------------------------------------------------------

@router.get("/schedule", response_model=ScheduleResponse)
async def get_schedule():
    """Return the global backup schedule."""
    return config_store.get_schedule()


@router.post("/schedule", response_model=ScheduleResponse)
async def update_schedule(data: ScheduleUpdate):
    """Update the global backup schedule."""
    result = config_store.save_schedule(enabled=data.enabled, cron=data.cron)
    await sync_scheduled_jobs()
    return result
