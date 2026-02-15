from pydantic import BaseModel
from app.models import AccountStatus, BackupStatus, DriveConfigMode, SyncPolicy


class AccountCreate(BaseModel):
    apple_id: str
    password: str


class AccountResponse(BaseModel):
    apple_id: str
    status: AccountStatus
    status_message: str | None = None


class TwoFactorSubmit(BaseModel):
    code: str


class SmsSendRequest(BaseModel):
    device_index: int


class TwoStepSubmit(BaseModel):
    device_index: int
    code: str


class BackupConfigCreate(BaseModel):
    backup_drive: bool = False
    backup_photos: bool = False
    drive_config_mode: DriveConfigMode = DriveConfigMode.SIMPLE
    drive_folders_simple: list[str] | None = None
    drive_folders_advanced: str | None = None
    photos_include_family: bool = False
    drive_sync_policy: SyncPolicy = SyncPolicy.DELETE
    photos_sync_policy: SyncPolicy = SyncPolicy.KEEP
    exclusions: list[str] | None = None
    destination: str = ""


class BackupConfigResponse(BaseModel):
    apple_id: str
    backup_drive: bool
    backup_photos: bool
    drive_config_mode: DriveConfigMode
    drive_folders_simple: list[str] | None = None
    drive_folders_advanced: str | None = None
    photos_include_family: bool
    drive_sync_policy: SyncPolicy = SyncPolicy.DELETE
    photos_sync_policy: SyncPolicy = SyncPolicy.KEEP
    exclusions: list[str] | None = None
    destination: str
    last_backup_status: BackupStatus = BackupStatus.IDLE
    last_backup_at: str | None = None
    last_backup_message: str | None = None
    last_backup_stats: dict | None = None


class ScheduleUpdate(BaseModel):
    enabled: bool = False
    cron: str = "0 2 * * *"


class ScheduleResponse(BaseModel):
    enabled: bool
    cron: str


class BackupTriggerResponse(BaseModel):
    message: str
    apple_id: str


class DriveFolderInfo(BaseModel):
    name: str
    type: str  # "folder" or "file"
    size: int | None = None
