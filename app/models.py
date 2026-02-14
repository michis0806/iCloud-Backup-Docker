import enum
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, Text, Enum, ForeignKey, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AccountStatus(str, enum.Enum):
    PENDING = "pending"
    REQUIRES_2FA = "requires_2fa"
    AUTHENTICATED = "authenticated"
    ERROR = "error"


class BackupStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class DriveConfigMode(str, enum.Enum):
    SIMPLE = "simple"
    ADVANCED = "advanced"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    apple_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus), default=AccountStatus.PENDING
    )
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    backup_configs: Mapped[list["BackupConfig"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Account {self.apple_id}>"


class BackupConfig(Base):
    __tablename__ = "backup_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)

    # What to backup
    backup_drive: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_photos: Mapped[bool] = mapped_column(Boolean, default=False)

    # iCloud Drive settings
    drive_config_mode: Mapped[DriveConfigMode] = mapped_column(
        Enum(DriveConfigMode), default=DriveConfigMode.SIMPLE
    )
    # Simple mode: JSON list of selected top-level folder names
    drive_folders_simple: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Advanced mode: raw text configuration (one path per line)
    drive_folders_advanced: Mapped[str | None] = mapped_column(Text, nullable=True)

    # iCloud Photos settings
    photos_include_family: Mapped[bool] = mapped_column(Boolean, default=False)

    # Exclusions (JSON list of glob/path patterns)
    exclusions: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Backup destination (relative to /backups)
    destination: Mapped[str] = mapped_column(String(500), nullable=False)

    # Scheduling
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Status tracking
    last_backup_status: Mapped[BackupStatus] = mapped_column(
        Enum(BackupStatus), default=BackupStatus.IDLE
    )
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_backup_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_backup_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    account: Mapped["Account"] = relationship(back_populates="backup_configs")

    def __repr__(self) -> str:
        return f"<BackupConfig account={self.account_id} drive={self.backup_drive} photos={self.backup_photos}>"
