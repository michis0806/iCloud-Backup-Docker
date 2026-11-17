"""Production entry points must use the bounded worker for the entire job."""
import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException

from app import config_store
from app.routers import backup as routes
from app.services import backup_service, backup_worker, scheduler


@pytest.fixture
def configured_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "_CONFIG_FILE", tmp_path / "config.yaml")
    accounts = ["first@example.com", "second@example.com"]
    for apple_id in accounts:
        config_store.add_account(apple_id, status="authenticated")
        config_store.save_backup_config(apple_id, {
            "backup_photos": True, "backup_notes": True, "backup_reminders": True,
        })
    monkeypatch.setattr(backup_worker, "try_reserve", lambda account: True)
    monkeypatch.setattr(backup_worker, "release", lambda account: None)
    monkeypatch.setattr(scheduler, "notify_backup_result", lambda *args: None)
    monkeypatch.setattr(routes, "notify_backup_result", lambda *args: None)
    # A scan here would escape the subprocess deadline and block the event loop.
    def unbounded_scan(*args):
        pytest.fail("Storage scan must run inside the watchdog worker")
    monkeypatch.setattr(backup_service, "get_backup_storage_stats", unbounded_scan)
    return accounts


@pytest.mark.asyncio
async def test_scheduled_batch_continues_after_timeout(configured_accounts, monkeypatch):
    calls = []

    def worker(apple_id, **kwargs):
        calls.append((apple_id, kwargs))
        if apple_id == configured_accounts[0]:
            return {"success": False, "message": "watchdog timeout", "timed_out": True}
        return {"success": True, "message": "OK", "storage": {"photos": {"count": 2}}}

    monkeypatch.setattr(backup_worker, "run_backup", worker)
    await scheduler._run_all_backups()

    assert [account for account, _ in calls] == configured_accounts
    assert all(kwargs["reserved"] for _, kwargs in calls)
    first, second = [config_store.get_backup_config(a) for a in configured_accounts]
    assert first["last_backup_status"] == "error"
    assert first["last_backup_message"] == "watchdog timeout"
    assert second["last_backup_status"] == "success"
    assert second["last_backup_stats"]["storage"]["photos"]["count"] == 2
    for cfg in (first, second):
        assert datetime.fromisoformat(cfg["last_backup_started_at"]).utcoffset().total_seconds() == 0
        assert datetime.fromisoformat(cfg["last_backup_at"]).utcoffset().total_seconds() == 0
        assert cfg["last_backup_duration_seconds"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("all_accounts", [False, True])
async def test_manual_runs_use_worker_for_scan_and_refresh(configured_accounts, monkeypatch, all_accounts):
    calls, tasks = [], []
    create_task = asyncio.create_task

    def capture(coroutine):
        task = create_task(coroutine)
        tasks.append(task)
        return task

    def worker(apple_id, **kwargs):
        calls.append((apple_id, kwargs))
        return {"success": False, "message": "watchdog timeout", "timed_out": True}

    monkeypatch.setattr(backup_worker, "run_backup", worker)
    monkeypatch.setattr(routes.asyncio, "create_task", capture)
    if all_accounts:
        await routes.trigger_all_backups()
    else:
        await routes.trigger_backup(configured_accounts[0])
    await asyncio.gather(*tasks)

    expected = configured_accounts if all_accounts else configured_accounts[:1]
    assert sorted(account for account, _ in calls) == sorted(expected)
    for account, kwargs in calls:
        assert kwargs["reserved"] and kwargs["refresh_storage"]
        assert kwargs["backup_notes"] and kwargs["backup_reminders"]
        assert config_store.get_backup_config(account)["last_backup_status"] == "error"


@pytest.mark.asyncio
async def test_duplicate_does_not_overwrite_persisted_status(configured_accounts, monkeypatch):
    account = configured_accounts[0]
    config_store.update_backup_status(account, "running", message="Existing run")
    monkeypatch.setattr(backup_worker, "try_reserve", lambda account: False)
    with pytest.raises(HTTPException) as exc:
        await routes.trigger_backup(account)
    assert exc.value.status_code == 409
    await scheduler._run_backup_job(account)
    cfg = config_store.get_backup_config(account)
    assert cfg["last_backup_status"] == "running"
    assert cfg["last_backup_message"] == "Existing run"
