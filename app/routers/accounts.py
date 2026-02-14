"""API routes for iCloud account management."""

import logging

from fastapi import APIRouter, HTTPException

from app import config_store
from app.schemas import AccountCreate, AccountResponse, TwoFactorSubmit
from app.services import icloud_service

log = logging.getLogger("icloud-backup")
router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
async def list_accounts():
    return config_store.list_accounts()


@router.post("", response_model=AccountResponse)
async def add_account(data: AccountCreate):
    # Check for duplicate
    if config_store.get_account(data.apple_id) is not None:
        raise HTTPException(status_code=400, detail="Account existiert bereits.")

    # Attempt authentication (password is only used here, not stored)
    auth_result = icloud_service.authenticate(data.apple_id, data.password)

    status = auth_result["status"]
    message = auth_result["message"]

    try:
        account = config_store.add_account(data.apple_id, status=status, status_message=message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return account


@router.post("/{apple_id}/2fa", response_model=AccountResponse)
async def submit_2fa(apple_id: str, data: TwoFactorSubmit):
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    result = icloud_service.submit_2fa_code(apple_id, data.code)

    updated = config_store.update_account_status(
        apple_id,
        status=result["status"],
        status_message=result["message"],
    )
    return updated


@router.post("/{apple_id}/reconnect", response_model=AccountResponse)
async def reconnect_account(apple_id: str):
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    # Reconnect using saved session tokens – no password needed
    auth_result = icloud_service.authenticate(apple_id)

    updated = config_store.update_account_status(
        apple_id,
        status=auth_result["status"],
        status_message=auth_result["message"],
    )
    return updated


@router.delete("/{apple_id}")
async def delete_account(apple_id: str):
    if not config_store.delete_account(apple_id):
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    icloud_service.disconnect(apple_id)
    return {"message": "Account gelöscht."}


@router.get("/{apple_id}/drive-folders")
async def get_drive_folders(apple_id: str):
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")
    if account["status"] != "authenticated":
        raise HTTPException(status_code=400, detail="Account nicht authentifiziert.")

    folders = icloud_service.get_drive_folders(apple_id)
    return folders
