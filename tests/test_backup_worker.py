"""Exercise the supervisor over real process and socket boundaries."""
import logging
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.i18n import t
from app.services import backup_service, backup_worker as worker, icloud_service


ACCOUNT = "worker@example.com"


def _fake_process(monkeypatch, code):
    children = []

    def spawn(channel):
        p = subprocess.Popen(
            [sys.executable, "-c", code, str(channel.fileno())],
            pass_fds=(channel.fileno(),),
        )
        children.append(p)
        return p

    monkeypatch.setattr(worker, "_spawn", spawn)
    return children


def test_timeout_reaps_stalled_writer_and_accepts_next_run(monkeypatch):
    children = _fake_process(monkeypatch, """
import signal, socket, sys, time
s = socket.socket(fileno=int(sys.argv[1]))
s.recv(65536)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
s.sendall(b'["progress",')  # An incomplete frame cannot block the deadline.
time.sleep(3600)
""")
    monkeypatch.setattr(worker.settings, "backup_timeout_seconds", 0.5)
    before = time.monotonic()
    result = worker.run_backup(ACCOUNT)
    assert time.monotonic() - before < 4
    assert result["message"] == t("backup.worker_timeout", seconds=0.5)
    assert result["success"] is False
    assert children[0].poll() == -signal.SIGKILL
    assert worker.try_reserve(ACCOUNT)
    worker.release(ACCOUNT)


def test_pending_kernel_exit_keeps_only_affected_account_fenced(monkeypatch):
    class Pending:
        returncode = None

        def poll(self):
            return self.returncode

    process = Pending()
    assert worker.try_reserve(ACCOUNT)
    worker._jobs[ACCOUNT].process = process
    worker.release(ACCOUNT)
    assert not worker.try_reserve(ACCOUNT)
    other = "other@example.com"
    assert worker.try_reserve(other)
    worker.release(other)
    process.returncode = -signal.SIGKILL
    assert worker.try_reserve(ACCOUNT)
    worker.release(ACCOUNT)


def test_pending_parent_challenge_survives_worker_preflight(monkeypatch):
    pending = object()
    monkeypatch.setattr(icloud_service, "get_pending_2fa_session", lambda apple_id: pending)
    monkeypatch.setattr(worker, "_spawn", lambda channel: pytest.fail("unexpected login"))
    result = worker.run_backup(ACCOUNT)
    assert result["auth_expired"] and not result["success"]
    assert backup_service.get_progress(ACCOUNT) is None


def test_child_relays_logs_and_progress_and_cancels(monkeypatch, caplog):
    children = _fake_process(monkeypatch, """
import logging, socket, sys, time
from app.services import backup_worker as w, backup_service as b
def run(apple_id, config_id):
    logging.getLogger("icloud-backup").warning("worker log message")
    b._set_progress(config_id, {"phase": "drive", "downloaded": 2})
    while not b._is_cancelled(config_id):
        time.sleep(.02)
    return {"success": False, "message": "cooperative"}
b.run_backup = run
w._child(socket.socket(fileno=int(sys.argv[1])))
""")
    with ThreadPoolExecutor(1) as executor, caplog.at_level(logging.WARNING):
        future = executor.submit(worker.run_backup, ACCOUNT)
        try:
            deadline = time.monotonic() + 8
            while (backup_service.get_progress(ACCOUNT) or {}).get("phase") != "drive":
                assert time.monotonic() < deadline and not future.done()
                time.sleep(0.03)
            assert "worker log message" in caplog.text
            assert worker.run_backup(ACCOUNT)["skipped_already_running"]
            assert backup_service.request_cancel(ACCOUNT)
            assert future.result(timeout=4)["message"] == t("backup.cancelled_summary")
            assert children[0].poll() == 0
            assert backup_service.get_progress(ACCOUNT) is None
        finally:
            worker.shutdown_workers()


def test_final_storage_and_cache_fetch_go_through_worker(monkeypatch, tmp_path):
    saved = []
    monkeypatch.setattr(worker.storage_cache, "save_cache", lambda account, data: saved.append((account, data)))
    _fake_process(monkeypatch, """
import socket, sys
from app.services import backup_worker as w, backup_service as b
b.run_backup = lambda apple_id, **kwargs: {"success": True, "message": "OK"}
b.get_backup_storage_stats = lambda dest: {"dest": dest}
w.icloud_service.get_storage_usage = lambda account: {"bytes": 10}
w._child(socket.socket(fileno=int(sys.argv[1])))
""")
    assert worker.run_backup(ACCOUNT, refresh_storage=True)["storage"] == {"dest": "worker_at_example_com"}
    assert saved == [(ACCOUNT, {"bytes": 10})]


def test_unexpected_worker_exit_becomes_a_job_error(monkeypatch):
    _fake_process(monkeypatch, """
import os, socket, sys
socket.socket(fileno=int(sys.argv[1])).recv(65536)
os._exit(12)
""")
    result = worker.run_backup(ACCOUNT)
    assert result == {"success": False, "message": t("backup.worker_exited", code=12)}


def test_final_storage_scan_is_subject_to_job_deadline(monkeypatch, tmp_path):
    marker = tmp_path / "scan-started"
    children = _fake_process(monkeypatch, f"""
import pathlib, socket, sys, time
from app.services import backup_worker as w, backup_service as b
b.run_backup = lambda apple_id, **kwargs: {{"success": True, "message": "OK"}}
def scan(dest):
    pathlib.Path({str(marker)!r}).touch()
    time.sleep(3600)
b.get_backup_storage_stats = scan
w._child(socket.socket(fileno=int(sys.argv[1])))
""")
    monkeypatch.setattr(worker.settings, "backup_timeout_seconds", 3)
    result = worker.run_backup(ACCOUNT)
    assert marker.exists()
    assert result["timed_out"] and not result["success"]
    assert children[0].poll() is not None


def test_orphan_worker_exits_when_supervisor_channel_closes(tmp_path):
    import socket
    marker = tmp_path / "scan-started"
    parent, child = socket.socketpair()
    with parent, child:
        process = subprocess.Popen([sys.executable, "-c", f"""
import pathlib, socket, sys, time
from app.services import backup_worker as w, backup_service as b
def run(*args, **kwargs):
    pathlib.Path({str(marker)!r}).touch()
    time.sleep(3600)
b.run_backup = run
w._child(socket.socket(fileno=int(sys.argv[1])))
""", str(child.fileno())], pass_fds=(child.fileno(),))
        try:
            child.close()
            worker._send(parent, [ACCOUNT, {}, False, None])
            deadline = time.monotonic() + 8
            while not marker.exists():
                assert time.monotonic() < deadline and process.poll() is None
                time.sleep(.03)
            parent.close()
            assert process.wait(timeout=3) == 1
        finally:
            process.kill()
            process.wait(timeout=3)
