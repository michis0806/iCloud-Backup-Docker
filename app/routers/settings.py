"""API routes for global application settings (notifications, ...)."""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import config_store
from app.services import notification

log = logging.getLogger("icloud-backup")
router = APIRouter(prefix="/api/settings", tags=["settings"])


class NotificationSettings(BaseModel):
    pushover_enabled: bool = False
    pushover_api_token: str = ""
    pushover_user_key: str = ""
    pushover_devices: str = ""


def _redact(settings_dict: dict) -> dict:
    """Never ship the full Pushover API token/user key to the browser."""
    out = dict(settings_dict)
    for key in ("pushover_api_token", "pushover_user_key"):
        value = out.get(key) or ""
        if value:
            out[key] = "•" * 8 + value[-4:] if len(value) > 4 else "•" * len(value)
    return out


class NotificationSettingsResponse(BaseModel):
    pushover_enabled: bool
    pushover_api_token_set: bool
    pushover_user_key_set: bool
    pushover_api_token_hint: str = ""
    pushover_user_key_hint: str = ""
    pushover_devices: str = ""


def _to_response(data: dict) -> NotificationSettingsResponse:
    redacted = _redact(data)
    return NotificationSettingsResponse(
        pushover_enabled=bool(data.get("pushover_enabled")),
        pushover_api_token_set=bool(data.get("pushover_api_token")),
        pushover_user_key_set=bool(data.get("pushover_user_key")),
        pushover_api_token_hint=redacted.get("pushover_api_token", "") if data.get("pushover_api_token") else "",
        pushover_user_key_hint=redacted.get("pushover_user_key", "") if data.get("pushover_user_key") else "",
        pushover_devices=data.get("pushover_devices") or "",
    )


@router.get("/notifications", response_model=NotificationSettingsResponse)
async def get_notification_settings():
    return _to_response(config_store.get_notifications())


@router.post("/notifications", response_model=NotificationSettingsResponse)
async def update_notification_settings(data: NotificationSettings):
    current = config_store.get_notifications()
    merged = dict(current)
    merged["pushover_enabled"] = data.pushover_enabled
    merged["pushover_devices"] = data.pushover_devices
    # Only overwrite secrets when the client actually sends a new value.
    # An empty string means "leave the stored secret untouched".
    if data.pushover_api_token:
        merged["pushover_api_token"] = data.pushover_api_token
    if data.pushover_user_key:
        merged["pushover_user_key"] = data.pushover_user_key
    saved = config_store.save_notifications(merged)
    return _to_response(saved)


class NotificationTestRequest(BaseModel):
    backend: Literal["pushover"]


class NotificationTestResponse(BaseModel):
    success: bool
    message: str


@router.post("/notifications/test", response_model=NotificationTestResponse)
async def test_notification(data: NotificationTestRequest):
    """Send a one-off test notification via the selected backend."""
    if data.backend == "pushover":
        result = await asyncio.to_thread(notification.test_pushover)
    else:  # pragma: no cover – pydantic already enforces this
        raise HTTPException(status_code=400, detail="Unbekannter Backend-Typ.")
    return NotificationTestResponse(**result)
