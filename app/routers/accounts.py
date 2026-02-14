"""API routes for iCloud account management."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Account, AccountStatus, BackupConfig
from app.schemas import AccountCreate, AccountResponse, TwoFactorSubmit
from app.services import icloud_service

log = logging.getLogger("icloud-backup")
router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account).order_by(Account.created_at))
    return result.scalars().all()


@router.post("", response_model=AccountResponse)
async def add_account(data: AccountCreate, db: AsyncSession = Depends(get_db)):
    # Check for duplicate
    existing = await db.execute(
        select(Account).where(Account.apple_id == data.apple_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Account existiert bereits.")

    account = Account(apple_id=data.apple_id)

    # Attempt authentication (password is only used here, not stored)
    auth_result = icloud_service.authenticate(data.apple_id, data.password)

    if auth_result["status"] == "requires_2fa":
        account.status = AccountStatus.REQUIRES_2FA
    elif auth_result["status"] == "authenticated":
        account.status = AccountStatus.AUTHENTICATED
    else:
        account.status = AccountStatus.ERROR

    account.status_message = auth_result["message"]
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@router.post("/{account_id}/2fa", response_model=AccountResponse)
async def submit_2fa(
    account_id: int,
    data: TwoFactorSubmit,
    db: AsyncSession = Depends(get_db),
):
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    result = icloud_service.submit_2fa_code(account.apple_id, data.code)

    if result["status"] == "authenticated":
        account.status = AccountStatus.AUTHENTICATED
    else:
        account.status = AccountStatus.ERROR

    account.status_message = result["message"]
    account.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(account)
    return account


@router.post("/{account_id}/reconnect", response_model=AccountResponse)
async def reconnect_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
):
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    # Reconnect using saved session tokens – no password needed
    auth_result = icloud_service.authenticate(account.apple_id)

    if auth_result["status"] == "requires_2fa":
        account.status = AccountStatus.REQUIRES_2FA
    elif auth_result["status"] == "authenticated":
        account.status = AccountStatus.AUTHENTICATED
    else:
        account.status = AccountStatus.ERROR

    account.status_message = auth_result["message"]
    account.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(account)
    return account


@router.delete("/{account_id}")
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")

    icloud_service.disconnect(account.apple_id)
    await db.delete(account)
    await db.commit()
    return {"message": "Account gelöscht."}


@router.get("/{account_id}/drive-folders")
async def get_drive_folders(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")
    if account.status != AccountStatus.AUTHENTICATED:
        raise HTTPException(status_code=400, detail="Account nicht authentifiziert.")

    folders = icloud_service.get_drive_folders(account.apple_id)
    return folders
