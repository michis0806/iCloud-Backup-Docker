"""Unit tests for the iCloud auth helper service."""

from unittest.mock import Mock, PropertyMock

from app.services import icloud_service
from tests.test_apple_auth import api  # shared offline pyicloud fixture


def test_get_trusted_devices_reads_hsa2_numbers_from_auth_endpoint(monkeypatch, api):
    apple_id = "sms@icloud.com"
    api._auth_data = {}
    api.session.get.return_value.json.return_value = {
        "trustedPhoneNumbers": [
            {"id": 1, "numberWithDialCode": "+49 111", "pushMode": "sms"},
            {"id": 2, "numberWithDialCode": "+49 222", "pushMode": "voice"},
        ]
    }
    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    monkeypatch.setattr(icloud_service, "_trusted_devices", {})
    assert icloud_service.get_trusted_devices(apple_id) == [
        {"index": 0, "name": "SMS an +49 111", "phone": "+49 111"},
        {"index": 1, "name": "SMS an +49 222", "phone": "+49 222"},
    ]
    api.session.put.assert_not_called()
    api.session.post.assert_not_called()


def test_send_sms_code_explicitly_uses_sms_not_phone_push_mode(monkeypatch, api):
    apple_id = "sms@icloud.com"
    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    monkeypatch.setattr(icloud_service, "_trusted_devices", {apple_id: api.trusted_phones()})
    result = icloud_service.send_sms_code(apple_id, 0)
    assert result == {"success": True, "message": "SMS-Code gesendet."}
    assert api.session.put.call_args.kwargs["json"] == {"phoneNumber": {"id": 7}, "mode": "sms"}
    api.session.get.assert_not_called()


def test_submit_2sa_code_for_hsa2_posts_phone_securitycode(monkeypatch, api):
    apple_id = "sms@icloud.com"
    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    monkeypatch.setattr(icloud_service, "_trusted_devices", {apple_id: api.trusted_phones()})
    icloud_service.send_sms_code(apple_id, 0)
    result = icloud_service.submit_2sa_code(apple_id, 0, "123456")
    assert result["status"] == "authenticated"
    api.trust_session.assert_called_once()
    assert api.session.post.call_args.args[0].endswith("/phone/securitycode")
    assert api.session.post.call_args.kwargs["json"] == {
        "phoneNumber": {"id": 7}, "securityCode": {"code": "123456"}, "mode": "sms",
    }


def test_reconnect_preserves_pending_challenge_even_with_password(monkeypatch, api):
    apple_id = "sms@icloud.com"
    api.request_challenge("push")
    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    factory = Mock(side_effect=AssertionError("must not start competing login"))
    monkeypatch.setattr(icloud_service, "PyiCloudService", factory)
    assert icloud_service.authenticate(apple_id, "password")["status"] == "requires_2fa"
    assert not icloud_service.check_connection(apple_id)["valid"]
    factory.assert_not_called()
    api.session.get.assert_called_once()


def test_wrong_sms_number_cannot_change_the_validation_route(monkeypatch, api):
    apple_id = "sms@icloud.com"
    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    monkeypatch.setattr(icloud_service, "_trusted_devices", {apple_id: api.trusted_phones()})
    icloud_service.send_sms_code(apple_id, 0)
    assert icloud_service.submit_2sa_code(apple_id, 1, "123456")["status"] == "error"
    api.session.post.assert_not_called()


def test_connection_check_recovers_existing_trusted_session_without_login(monkeypatch, api):
    apple_id = "sms@icloud.com"
    api.request_challenge("sms", "7")
    api._validate_token.return_value = {
        "hsaTrustedBrowser": True, "hsaChallengeRequired": False,
        "webservices": {"drivews": {"url": "https://example.com/drive"}},
    }
    drive = Mock()
    monkeypatch.setattr(type(api), "drive", PropertyMock(return_value=drive))
    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    factory = Mock(side_effect=AssertionError("must not start competing login"))
    monkeypatch.setattr(icloud_service, "PyiCloudService", factory)
    assert icloud_service.check_connection(apple_id)["valid"]
    assert icloud_service._sessions[apple_id] is api
    assert not api.requires_2fa
    drive.dir.assert_called_once()
    factory.assert_not_called()
    api.trust_session.assert_not_called()
    api.session.post.assert_not_called()
    api.session.put.assert_called_once()


def test_connection_check_does_not_report_success_when_drive_probe_fails(monkeypatch, api):
    apple_id = "sms@icloud.com"
    api.request_challenge("sms", "7")
    api._validate_token.return_value = {"hsaTrustedBrowser": True}
    drive = Mock()
    drive.dir.side_effect = RuntimeError("offline")
    monkeypatch.setattr(type(api), "drive", PropertyMock(return_value=drive))
    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    assert not icloud_service.check_connection(apple_id)["valid"]


def test_get_notes_clears_stale_attachment_url_cache(monkeypatch):
    """Signed attachment URLs expire; a reused session must not serve them."""
    apple_id = "notes@icloud.com"

    class DummyNotes:
        def __init__(self):
            self._attachment_meta_cache = {"att-1": object()}

        def folders(self):
            return []

        def iter_all(self):
            return []

    class DummyNotesApi:
        def __init__(self):
            self.notes = DummyNotes()

    api = DummyNotesApi()
    monkeypatch.setattr(icloud_service, "get_session", lambda _: api)

    result = icloud_service.get_notes(apple_id)

    assert result == {"folders": [], "notes": []}
    assert api.notes._attachment_meta_cache == {}


class _FakeApiError(Exception):
    """Mimics pyicloud's NotesApiError/RemindersApiError (message + payload)."""

    def __init__(self, message, payload=None):
        super().__init__(message)
        self.payload = payload


def test_get_reminders_missing_zone_returns_empty(monkeypatch):
    """An account without a Reminders zone is empty, not a backup failure."""

    class DummyReminders:
        def lists(self):
            # Reminders surface the missing zone as a wrapped validation error;
            # the ZONE_NOT_FOUND marker only lives in the payload.
            raise _FakeApiError(
                "Changes response validation failed",
                payload={
                    "zones": [
                        {"serverErrorCode": "ZONE_NOT_FOUND", "reason": "Zone does not exist"}
                    ]
                },
            )

        def reminders(self):
            return []

    class DummyApi:
        reminders = DummyReminders()

    monkeypatch.setattr(icloud_service, "get_session", lambda _: DummyApi())

    assert icloud_service.get_reminders("empty@icloud.com") == {"lists": [], "reminders": []}


def test_get_reminders_real_error_returns_none(monkeypatch):
    class DummyReminders:
        def lists(self):
            raise _FakeApiError("Internal server error (500)")

        def reminders(self):
            return []

    class DummyApi:
        reminders = DummyReminders()

    monkeypatch.setattr(icloud_service, "get_session", lambda _: DummyApi())

    assert icloud_service.get_reminders("broken@icloud.com") is None


def test_get_notes_missing_zone_returns_empty(monkeypatch):
    """An account without a Notes zone is empty, not a backup failure."""

    class DummyNotes:
        def __init__(self):
            self._attachment_meta_cache = {}

        def folders(self):
            # Notes carry the marker directly in the 404 message.
            raise _FakeApiError(
                'Not Found (404): { "serverErrorCode" : "ZONE_NOT_FOUND", '
                '"reason" : "Zone does not exist" }'
            )

        def iter_all(self):
            return []

    class DummyApi:
        def __init__(self):
            self.notes = DummyNotes()

    monkeypatch.setattr(icloud_service, "get_session", lambda _: DummyApi())

    assert icloud_service.get_notes("empty@icloud.com") == {"folders": [], "notes": []}


def test_get_notes_real_error_returns_none(monkeypatch):
    class DummyNotes:
        def __init__(self):
            self._attachment_meta_cache = {}

        def folders(self):
            raise _FakeApiError("Service temporarily unavailable (503)")

        def iter_all(self):
            return []

    class DummyApi:
        def __init__(self):
            self.notes = DummyNotes()

    monkeypatch.setattr(icloud_service, "get_session", lambda _: DummyApi())

    assert icloud_service.get_notes("broken@icloud.com") is None
