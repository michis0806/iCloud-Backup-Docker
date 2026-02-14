"""Enums shared across the application."""

import enum


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
