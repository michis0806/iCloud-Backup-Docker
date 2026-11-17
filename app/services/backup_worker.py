"""Supervise backup I/O in a disposable process, including the final storage scan."""

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from app.config import settings
from app.i18n import t
from app.services import backup_service, icloud_service, storage_cache

log = logging.getLogger("icloud-backup")
_POLL_SECONDS = 0.25
_CANCEL_GRACE_SECONDS = 5
_REAP_SECONDS = 1


@dataclass
class _Job:
    process: subprocess.Popen | None = None
    supervising: bool = False
    released: bool = False
    stopping: bool = False


_lock = threading.Lock()
_jobs: dict[str, _Job] = {}


def _discard_finished(apple_id: str, job: _Job) -> None:
    # Called under _lock. SIGKILL can remain pending in a CIFS kernel call.
    # Fence that account until waitpid confirms its old writer has exited.
    if job.released and not job.supervising and (
        job.process is None or job.process.poll() is not None
    ):
        _jobs.pop(apple_id)


def try_reserve(apple_id: str) -> bool:
    """Claim this account before changing its persisted run status."""
    with _lock:
        if apple_id in _jobs:
            _discard_finished(apple_id, _jobs[apple_id])
        if apple_id in _jobs:
            return False
        _jobs[apple_id] = _Job()
        backup_service._register_cancel_event(apple_id)
        backup_service._set_progress(apple_id, {
            "phase": "starting", "folder": "", "current_file": "",
            "downloaded": 0, "skipped": 0, "errors": 0,
        })
        return True


def release(apple_id: str) -> None:
    with _lock:
        job = _jobs.get(apple_id)
        if job is not None:
            job.released = True
            if job.supervising:  # The asyncio caller was cancelled mid-run.
                job.stopping = True
            backup_service._clear_progress(apple_id)
            _discard_finished(apple_id, job)


def shutdown_workers() -> None:
    """Signal every worker before waiting, with one shared shutdown budget."""
    with _lock:
        processes = []
        for job in _jobs.values():
            job.stopping = True
            if job.process is not None and job.process.poll() is None:
                job.process.kill()
                processes.append(job.process)
    deadline = time.monotonic() + _REAP_SECONDS
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass


def _send(sock: socket.socket, data) -> None:
    sock.sendall(json.dumps(data, ensure_ascii=True).encode("ascii") + b"\n")


def _spawn(channel: socket.socket) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "app.services.backup_worker", str(channel.fileno())],
        pass_fds=(channel.fileno(),),
    )


def _failed(message: str, **flags) -> dict:
    return {"success": False, "message": message, **flags}


def _supervise(apple_id, job, channel, deadline):
    pending = bytearray()
    result = None
    cancel_deadline = None
    channel.settimeout(_POLL_SECONDS)
    while True:
        now = time.monotonic()
        if job.stopping or (cancel_deadline is not None and now >= cancel_deadline):
            return _failed(t("backup.cancelled_summary"))
        if now >= deadline:
            return _failed(t("backup.worker_timeout", seconds=settings.backup_timeout_seconds), timed_out=True)
        if cancel_deadline is None and backup_service._is_cancelled(apple_id):
            cancel_deadline = now + _CANCEL_GRACE_SECONDS
            try:
                channel.sendall(b"c")
            except OSError:
                pass
        try:
            chunk = channel.recv(64 * 1024)
        except socket.timeout:
            continue
        except ConnectionResetError:
            chunk = b""
        if not chunk:
            # A killed or crashed worker may close the channel without a result.
            # A process can also wedge during interpreter shutdown; bound waitpid.
            try:
                code = job.process.wait(timeout=min(_POLL_SECONDS, max(0, deadline - now)))
            except subprocess.TimeoutExpired:
                continue
            if cancel_deadline is not None or job.stopping:
                return _failed(t("backup.cancelled_summary"))
            if code == 0 and result is not None:
                return result
            return _failed(t("backup.worker_exited", code=code))
        pending.extend(chunk)
        records = pending.split(b"\n")
        pending = records.pop()
        for record in records:
            kind, value = json.loads(record)
            if kind == "log":
                name, level, message = value
                logging.getLogger(name).log(level, "%s", message)
            elif kind == "progress":
                backup_service._set_progress(apple_id, value)
            elif kind == "result":
                result = value


def run_backup(apple_id: str, *, reserved=False, refresh_storage=False, **kwargs) -> dict:
    """Run off the event loop. An explicit reservation lasts until release()."""
    if not reserved and not try_reserve(apple_id):
        return _failed(t("backup.already_running"), skipped_already_running=True)
    with _lock:
        job = _jobs[apple_id]
        job.supervising = True
    try:
        if icloud_service.get_pending_2fa_session(apple_id) is not None:
            # The challenge and its verifier live in the web process. A fresh
            # login from the worker would invalidate the code the user received.
            return _failed(t("icloud.2fa_pending"), auth_expired=True)
        deadline = time.monotonic() + settings.backup_timeout_seconds
        parent, child = socket.socketpair()
        with parent, child:
            with _lock:
                if job.stopping or backup_service._is_cancelled(apple_id):
                    return _failed(t("backup.cancelled_summary"))
                job.process = _spawn(child)
            child.close()
            parent.settimeout(settings.backup_timeout_seconds)
            try:
                _send(parent, [apple_id, kwargs, refresh_storage, icloud_service.get_user_record(apple_id)])
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                # Supervision reports the worker exit code or the elapsed deadline.
                pass
            result = _supervise(apple_id, job, parent, deadline)
            if not result["success"]:
                log.error(t("api.backup.failed_log"), apple_id, result["message"])
            if result.get("auth_expired") and icloud_service.get_pending_2fa_session(apple_id) is None:
                # A trusted session cached by the web UI has now expired. Its
                # next interactive login must reload cookies from disk.
                icloud_service.disconnect(apple_id)
            usage = result.pop("cloud_usage", None)
            if usage is not None:
                try:
                    storage_cache.save_cache(apple_id, usage)
                except Exception:
                    log.debug(t("api.backup.cache_refresh_failed"), apple_id, exc_info=True)
            return result
    finally:
        if job.process is not None and job.process.poll() is None:
            job.process.kill()
            try:
                job.process.wait(timeout=_REAP_SECONDS)
            except subprocess.TimeoutExpired:
                pass  # release() retains the reservation until a later poll succeeds.
        with _lock:
            job.supervising = False
            if job.released:
                backup_service._clear_progress(apple_id)
            _discard_finished(apple_id, job)
        if not reserved:
            release(apple_id)


def _child(channel: socket.socket) -> None:
    reader = channel.makefile("rb")
    apple_id, kwargs, refresh_storage, owner = json.loads(reader.readline())
    config_id = kwargs.setdefault("config_id", apple_id)
    if owner:
        icloud_service._user_records[apple_id] = owner
    send_lock = threading.Lock()

    def emit(kind, data):
        with send_lock:
            _send(channel, (kind, data))

    class ForwardLogs(logging.Handler):
        def emit(self, record):
            emit("log", (record.name, record.levelno, self.format(record)))

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        handlers=[ForwardLogs()], format="%(message)s", force=True,
    )
    # The control stream is independent from progress and log messages. EOF
    # also stops an orphaned writer if the web process dies unexpectedly.
    cancelled = threading.Event()
    if config_id is not None:
        backup_service._register_cancel_event(config_id)

    def monitor_parent():
        if reader.read(1) == b"c":
            cancelled.set()
            backup_service.request_cancel(config_id)
            reader.read(1)
        os._exit(1)

    threading.Thread(target=monitor_parent, daemon=True).start()

    def report_progress():
        previous = None
        while not finished.wait(_POLL_SECONDS):
            current = backup_service.get_progress(config_id)
            if current and current != previous:
                emit("progress", current)
                previous = current

    finished = threading.Event()
    threading.Thread(target=report_progress, daemon=True).start()
    try:
        result = backup_service.run_backup(apple_id, **kwargs)
        if not cancelled.is_set() and not result.get("auth_expired"):
            dest = kwargs.get("destination") or apple_id.replace("@", "_at_").replace(".", "_")
            result["storage"] = backup_service.get_backup_storage_stats(dest)
            if refresh_storage and not cancelled.is_set():
                try:
                    result["cloud_usage"] = icloud_service.get_storage_usage(apple_id)
                except Exception:
                    log.debug(t("api.backup.cache_refresh_failed"), apple_id, exc_info=True)
    except Exception as exc:
        result = _failed(str(exc))
    finally:
        finished.set()
    emit("result", result)
    # daemon control thread intentionally remains inside a buffered read. Exit
    # directly after sendall so interpreter teardown cannot contend on that I/O.
    os._exit(0)


if __name__ == "__main__":
    _child(socket.socket(fileno=int(sys.argv[1])))
