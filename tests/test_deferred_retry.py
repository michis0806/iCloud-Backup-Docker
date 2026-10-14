"""Deferred retry of aborted backup steps and iCloud error descriptions."""

import logging

import pytest
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
    PyiCloudServiceUnavailable,
)

from app.services import backup_service as bs

OK = {"downloaded": 1, "skipped": 0, "deleted": 0, "archived": 0, "errors": 0}
UNAVAILABLE = {"downloaded": 0, "skipped": 0, "errors": 1, "unavailable": True,
               "cause": "HTTP 503 Service Unavailable"}


class FakeResponse:
    status_code = 503
    reason = "Service Unavailable"
    url = "https://p1-photos.icloud.com/database/1/query?dsid=12345&clientId=abc"
    text = "X" * 5000


def _unavailable():
    try:
        try:
            raise PyiCloudAPIResponseException("Bad Gateway", code=503)
        except PyiCloudAPIResponseException as cause:
            cause.response = FakeResponse()
            raise PyiCloudServiceUnavailable("Photos service not available") from cause
    except PyiCloudServiceUnavailable as exc:
        return exc


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "_DEFERRED_RETRY_MIN_DELAY", 0)
    monkeypatch.setattr(bs.settings, "backup_path", tmp_path / "b", raising=False)
    monkeypatch.setattr(bs.settings, "archive_path", tmp_path / "a", raising=False)
    monkeypatch.setattr(bs.icloud_service, "check_connection", lambda a: {"valid": True})


def _run(**kw):
    return bs.run_backup(
        "a@b.c", backup_photos=True, backup_contacts=True, backup_calendar=True,
        destination="dest", **kw,
    )


def _patch_steps(monkeypatch, calls, photos):
    def mk(name, stats):
        def f(*a, **k):
            calls.append(name)
            return stats
        return f
    monkeypatch.setattr(bs, "run_photos_backup", lambda *a, **k: (calls.append("photos"), photos())[1])
    monkeypatch.setattr(bs, "run_contacts_backup", mk("contacts", OK))
    monkeypatch.setattr(bs, "run_calendar_backup", mk("calendar", OK))


def test_photos_recovers_on_deferred_retry(monkeypatch):
    calls, seq = [], [dict(UNAVAILABLE), {**OK, "downloaded": 7}]
    _patch_steps(monkeypatch, calls, lambda: seq.pop(0))
    result = _run()
    assert calls == ["photos", "contacts", "calendar", "photos"]
    assert result["success"] is True
    assert result["photos_stats"]["downloaded"] == 7
    assert "unavailable" not in result["photos_stats"]


def test_photos_unavailable_twice(monkeypatch):
    calls = []
    _patch_steps(monkeypatch, calls, lambda: dict(UNAVAILABLE))
    result = _run()
    assert calls == ["photos", "contacts", "calendar", "photos"]
    assert result["success"] is False
    assert result["photos_stats"]["unavailable"] is True
    assert "HTTP 503 Service Unavailable" in result["message"]
    assert bs.t("backup.service_photos") in result["message"]
    assert bs.t("backup.summary_contacts", count=1) in result["message"]


def test_generic_exception_retried_once(monkeypatch):
    calls = []
    _patch_steps(monkeypatch, calls, lambda: OK)

    def boom(*a, **k):
        calls.append("contacts")
        raise ConnectionError("network down")

    monkeypatch.setattr(bs, "run_contacts_backup", boom)
    result = _run()
    assert calls == ["photos", "contacts", "calendar", "contacts"]
    assert result["success"] is False
    assert result["contacts_stats"]["unavailable"] is True
    assert "network down" in result["message"]


@pytest.mark.parametrize("exc", [
    PyiCloudFailedLoginException("bad"), PyiCloud2FARequiredException("a@b.c", None),
])
def test_auth_exception_propagates(monkeypatch, exc):
    calls = []
    _patch_steps(monkeypatch, calls, lambda: OK)

    def auth(*a, **k):
        calls.append("contacts")
        raise exc

    monkeypatch.setattr(bs, "run_contacts_backup", auth)
    with pytest.raises(type(exc)):
        _run()
    assert calls == ["photos", "contacts"]


def test_cancel_during_deferred_wait(monkeypatch):
    calls = []
    _patch_steps(monkeypatch, calls, lambda: dict(UNAVAILABLE))
    monkeypatch.setattr(bs, "_DEFERRED_RETRY_MIN_DELAY", 30)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    monkeypatch.setattr(bs, "_is_cancelled", lambda cid: True)
    result = bs.run_backup(
        "a@b.c", backup_photos=True, backup_contacts=True, destination="dest",
        config_id="cfg",
    )
    # cancelled before any step ran -> nothing retried, job reported unsuccessful
    assert result["success"] is False
    assert calls.count("photos") <= 1


def test_cancel_during_wait_after_abort(monkeypatch):
    calls, state = [], {"cancel": False}
    _patch_steps(monkeypatch, calls, lambda: dict(UNAVAILABLE))
    monkeypatch.setattr(bs, "_DEFERRED_RETRY_MIN_DELAY", 30)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    monkeypatch.setattr(bs, "_is_cancelled", lambda cid: state["cancel"])

    def contacts(*a, **k):
        calls.append("contacts")
        state["cancel"] = True  # user cancels while the job is past the first pass
        return OK

    monkeypatch.setattr(bs, "run_contacts_backup", contacts)
    result = bs.run_backup(
        "a@b.c", backup_photos=True, backup_contacts=True, destination="dest",
        config_id="cfg",
    )
    assert calls == ["photos", "contacts"]
    assert result["success"] is False


class PhotosApi:
    calls = 0

    @property
    def photos(self):
        PhotosApi.calls += 1
        raise _unavailable()


def test_photos_single_attempt_no_reconcile(monkeypatch, caplog):
    PhotosApi.calls = 0
    monkeypatch.setattr(bs.icloud_service, "get_session", lambda a: PhotosApi())

    def forbidden(*a, **k):
        raise AssertionError("reconciliation/backup must not run")

    monkeypatch.setattr(bs, "_backup_photo_library", forbidden)
    with caplog.at_level(logging.WARNING, logger="icloud-backup"):
        stats = bs.run_photos_backup("a@b.c", "dest")
    assert PhotosApi.calls == 1
    assert stats["unavailable"] is True and stats["errors"] == 1
    assert stats["cause"] == "HTTP 503 Service Unavailable"
    # The full cause is handed to the orchestration, which logs it once.
    assert "503" in stats["detail"]


def test_describe_and_short_error():
    exc = _unavailable()
    text = bs._describe_icloud_error(exc, max_body=100)
    assert "PyiCloudServiceUnavailable" in text
    assert "PyiCloudAPIResponseException" in text
    assert "503" in text
    assert "/database/1/query" in text
    assert "dsid" not in text and "clientId" not in text
    assert text.count("X") == 100
    short = bs._short_icloud_error(exc)
    assert short == "HTTP 503 Service Unavailable"
    assert len(bs._short_icloud_error(ConnectionError("y" * 500))) <= 120
