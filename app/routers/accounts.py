"""API routes for iCloud account management."""

import logging

from fastapi import APIRouter, HTTPException

from app import config_store
from app.schemas import AccountCreate, AccountResponse, SmsSendRequest, TwoFactorSubmit, TwoStepSubmit
from app.services import icloud_service

log = logging.getLogger("icloud-backup")
router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
async def list_accounts():
    return config_store.list_accounts()


@router.get("/storage-stats")
async def storage_stats():
    """Return cached storage stats (file counts + sizes) per account, split by photos/drive.

    Stats are computed after each successful backup and stored in the config.
    """
    result = {}
    for acc_data in config_store.list_accounts():
        apple_id = acc_data["apple_id"]
        cfg = config_store.get_backup_config(apple_id)
        if cfg is None:
            continue
        last_stats = cfg.get("last_backup_stats") or {}
        storage = last_stats.get("storage")
        if storage:
            result[apple_id] = storage
    return result


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
        account = config_store.add_account(
            data.apple_id,
            status=status,
            status_message=message,
            token_refreshed=(status == "authenticated"),
        )
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
        token_refreshed=(result["status"] == "authenticated"),
    )
    return updated


@router.get("/{apple_id}/2fa/devices")
async def get_trusted_devices(apple_id: str):
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    devices = icloud_service.get_trusted_devices(apple_id)
    return devices


@router.post("/{apple_id}/2fa/sms")
async def send_sms_code(apple_id: str, data: SmsSendRequest):
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    result = icloud_service.send_sms_code(apple_id, data.device_index)
    return result


@router.post("/{apple_id}/2sa", response_model=AccountResponse)
async def submit_2sa(apple_id: str, data: TwoStepSubmit):
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    result = icloud_service.submit_2sa_code(apple_id, data.device_index, data.code)

    updated = config_store.update_account_status(
        apple_id,
        status=result["status"],
        status_message=result["message"],
        token_refreshed=(result["status"] == "authenticated"),
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
        token_refreshed=(auth_result["status"] == "authenticated"),
    )
    return updated


@router.post("/{apple_id}/check-connection")
async def check_connection(apple_id: str):
    """Check whether the iCloud session token is still valid.

    Performs a lightweight reconnect + API call to verify the session.
    Updates the account status accordingly.
    """
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    result = icloud_service.check_connection(apple_id)

    if result["valid"]:
        config_store.update_account_status(
            apple_id,
            status="authenticated",
            status_message=result["message"],
        )
    elif result["requires_2fa"]:
        config_store.update_account_status(
            apple_id,
            status="requires_2fa",
            status_message=result["message"],
        )
    else:
        config_store.update_account_status(
            apple_id,
            status="error",
            status_message=result["message"],
        )

    return result


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


@router.get("/{apple_id}/photo-libraries")
async def get_photo_libraries(apple_id: str):
    """Return available photo libraries (primary + shared/family) for the account."""
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")
    if account["status"] != "authenticated":
        raise HTTPException(status_code=400, detail="Account nicht authentifiziert.")

    libraries = icloud_service.get_photo_libraries(apple_id)

    # For each shared library, check if another account already claims it
    for lib in libraries:
        if lib["type"] == "shared":
            claimed_by = config_store.get_shared_library_owner(lib["id"], exclude_apple_id=apple_id)
            lib["claimed_by"] = claimed_by  # None or the apple_id that already backs it up

    return libraries
