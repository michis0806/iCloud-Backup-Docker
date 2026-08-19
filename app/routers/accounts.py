"""API routes for iCloud account management."""

import logging

from fastapi import APIRouter, HTTPException

from app import config_store
from app.schemas import AccountCreate, AccountResponse, ReconnectRequest, SmsSendRequest, TwoFactorSubmit, TwoStepSubmit
from app.services import icloud_service, storage_cache
from app.services.notification import notify_token_expired

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

    # Attempt authentication (password is only persisted when the user
    # opted in via remember_password – encrypted, see app/crypto.py)
    auth_result = icloud_service.authenticate(data.apple_id, data.password)

    status = auth_result["status"]
    message = auth_result["message"]

    # If 2FA is needed, explicitly ask Apple to push the code to trusted
    # devices so the user sees the prompt immediately on their phone/Mac.
    if status == "requires_2fa":
        api = icloud_service._sessions.get(data.apple_id)
        if api:
            icloud_service._request_device_push(api)

    try:
        account = config_store.add_account(
            data.apple_id,
            status=status,
            status_message=message,
            token_refreshed=(status == "authenticated"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Only store the password once Apple accepted it (login or 2FA stage).
    if data.remember_password and status in ("authenticated", "requires_2fa"):
        if config_store.set_account_password(data.apple_id, data.password):
            account["password_saved"] = True
        else:
            account["status_message"] = (
                f"{message} Hinweis: Passwort wurde nicht gespeichert – "
                "ICLOUD_SECRET_KEY ist nicht konfiguriert."
            )

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


@router.post("/{apple_id}/2fa/push")
async def request_2fa_push(apple_id: str, body: ReconnectRequest | None = None):
    """Re-trigger 2FA push notification by forcing a fresh authentication."""
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    password = body.password if body else None
    if not password:
        # Fall back to the stored (opt-in) password so the push works
        # without re-entering credentials.
        password = config_store.get_account_password(apple_id)
    result = icloud_service.request_2fa_push(apple_id, password=password)

    # Update account status if auth state changed
    if result.get("status"):
        config_store.update_account_status(
            apple_id,
            status=result["status"],
            status_message=result["message"],
            token_refreshed=(result["status"] == "authenticated"),
        )

    return result


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


@router.post("/{apple_id}/reconnect")
async def reconnect_account(apple_id: str, body: ReconnectRequest | None = None):
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    password = body.password if body else None
    entered_password = password
    if not password:
        # A pending 2FA session means a code is already underway – reuse it
        # and just re-trigger the push. Starting a competing login would
        # invalidate the code the user is about to enter.
        pending = icloud_service.get_pending_2fa_session(apple_id)
        if pending is not None and icloud_service._request_device_push(pending):
            updated = config_store.update_account_status(
                apple_id,
                status="requires_2fa",
                status_message="Zwei-Faktor-Code wurde erneut an Ihre Geräte gesendet.",
            )
            return dict(updated)
        # No pending session (or it went stale and the push failed):
        # fall back to the stored (opt-in) password for a fresh login.
        password = config_store.get_account_password(apple_id)

    auth_result = icloud_service.authenticate(apple_id, password=password)

    # If 2FA is needed, explicitly request Apple to send a push notification
    if auth_result["status"] == "requires_2fa":
        api = icloud_service._sessions.get(apple_id)
        if api:
            icloud_service._request_device_push(api)

    # Persist a manually entered password once Apple accepted it.
    if (
        entered_password
        and body is not None
        and body.remember_password
        and auth_result["status"] in ("authenticated", "requires_2fa")
    ):
        config_store.set_account_password(apple_id, entered_password)

    updated = config_store.update_account_status(
        apple_id,
        status=auth_result["status"],
        status_message=auth_result["message"],
        token_refreshed=(auth_result["status"] == "authenticated"),
    )
    result = dict(updated)
    if auth_result.get("requires_password"):
        result["requires_password"] = True
    return result


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
        if account["status"] == "authenticated":
            notify_token_expired(apple_id)
    else:
        config_store.update_account_status(
            apple_id,
            status="error",
            status_message=result["message"],
        )

    return result


@router.get("/{apple_id}/icloud-storage")
async def get_icloud_storage(apple_id: str, refresh: bool = False):
    """Return iCloud storage quota and per-media usage.

    Reads from the on-disk cache by default (populated after every backup).
    Set ``?refresh=true`` to force a fresh fetch from Apple.
    """
    account = config_store.get_account(apple_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    if refresh:
        if account["status"] != "authenticated":
            raise HTTPException(status_code=400, detail="Account nicht authentifiziert.")
        data = storage_cache.refresh(apple_id)
        if data is None:
            raise HTTPException(status_code=503, detail="Speicherinfo nicht verfügbar.")
        return data

    cached = storage_cache.load_cache(apple_id)
    if cached is not None:
        return cached

    # No cache yet – try a one-shot fetch so the UI has something to show.
    if account["status"] != "authenticated":
        raise HTTPException(status_code=503, detail="Speicherinfo nicht verfügbar.")
    data = storage_cache.refresh(apple_id)
    if data is None:
        raise HTTPException(status_code=503, detail="Speicherinfo nicht verfügbar.")
    return data


@router.delete("/{apple_id}/password")
async def delete_stored_password(apple_id: str):
    """Remove the stored (encrypted) account password."""
    if config_store.get_account(apple_id) is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")
    removed = config_store.clear_account_password(apple_id)
    return {"removed": removed, "message": "Gespeichertes Passwort entfernt." if removed else "Kein Passwort gespeichert."}


@router.delete("/{apple_id}")
async def delete_account(apple_id: str):
    if not config_store.delete_account(apple_id):
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    icloud_service.disconnect(apple_id)
    storage_cache.delete_cache(apple_id)
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
