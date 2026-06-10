"""Unit tests for the iCloud auth helper service."""

from app.services import icloud_service


class DummyResponse:
    def __init__(self, ok=True, status_code=200, json_data=None):
        self.ok = ok
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}

    def json(self):
        return self._json_data


class DummySession:
    def __init__(self, *, get_response=None, put_response=None, post_response=None):
        self.calls = []
        self._get_response = get_response or DummyResponse()
        self._put_response = put_response or DummyResponse()
        self._post_response = post_response or DummyResponse()

    def get(self, url, headers=None, **kwargs):
        self.calls.append(("get", url, headers or {}, kwargs))
        return self._get_response

    def put(self, url, json=None, headers=None, **kwargs):
        self.calls.append(("put", url, headers or {}, {"json": json, **kwargs}))
        return self._put_response

    def post(self, url, json=None, headers=None, **kwargs):
        self.calls.append(("post", url, headers or {}, {"json": json, **kwargs}))
        return self._post_response


class DummyApi:
    AUTH_ENDPOINT = "https://idmsa.apple.com/appleauth/auth"

    def __init__(self, *, requires_2fa=False, requires_2sa=False, auth_data=None, data=None, session=None):
        self.requires_2fa = requires_2fa
        self.requires_2sa = requires_2sa
        self._auth_data = auth_data if auth_data is not None else {}
        self.data = data if data is not None else {"dsInfo": {}}
        self.session_data = {"scnt": "test-scnt", "session_id": "test-session-id"}
        self.session = session or DummySession()
        self.trust_session_called = False

    def _get_auth_headers(self, overrides=None):
        return {"X-Test": "1", **(overrides or {})}

    def trust_session(self):
        self.trust_session_called = True
        self.requires_2fa = False
        return True


def test_get_trusted_devices_reads_hsa2_numbers_from_auth_endpoint(monkeypatch):
    apple_id = "sms@icloud.com"
    session = DummySession(
        get_response=DummyResponse(
            json_data={
                "trustedPhoneNumbers": [
                    {"id": 1, "numberWithDialCode": "+49 111", "pushMode": "sms"},
                    {"id": 2, "numberWithDialCode": "+49 222", "pushMode": "voice"},
                ]
            }
        )
    )
    api = DummyApi(requires_2fa=True, session=session)

    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    monkeypatch.setattr(icloud_service, "_trusted_devices", {})

    devices = icloud_service.get_trusted_devices(apple_id)

    assert devices == [
        {"index": 0, "name": "SMS an +49 111", "phone": "+49 111"},
        {"index": 1, "name": "SMS an +49 222", "phone": "+49 222"},
    ]
    assert icloud_service._trusted_devices[apple_id][1]["pushMode"] == "voice"

    method, url, headers, _ = session.calls[0]
    assert method == "get"
    assert url == DummyApi.AUTH_ENDPOINT
    assert headers["Accept"] == "application/json"
    assert headers["scnt"] == "test-scnt"
    assert headers["X-Apple-ID-Session-Id"] == "test-session-id"


def test_send_sms_code_uses_phone_push_mode(monkeypatch):
    apple_id = "sms@icloud.com"
    session = DummySession(put_response=DummyResponse(json_data={"trustedPhoneNumber": {"id": 7}}))
    api = DummyApi(requires_2fa=True, auth_data={}, session=session)

    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    monkeypatch.setattr(
        icloud_service,
        "_trusted_devices",
        {
            apple_id: [
                {"id": 7, "numberWithDialCode": "+49 777", "pushMode": "voice"},
            ]
        },
    )

    result = icloud_service.send_sms_code(apple_id, 0)

    assert result == {"success": True, "message": "SMS-Code gesendet."}
    assert api._auth_data["mode"] == "voice"
    assert api._auth_data["trustedPhoneNumber"]["id"] == 7

    method, url, headers, payload = session.calls[0]
    assert method == "put"
    assert url == f"{DummyApi.AUTH_ENDPOINT}/verify/phone"
    assert headers["Accept"] == "application/json"
    assert payload["json"] == {"phoneNumber": {"id": 7}, "mode": "voice"}


def test_submit_2sa_code_for_hsa2_posts_phone_securitycode(monkeypatch):
    apple_id = "sms@icloud.com"
    session = DummySession(post_response=DummyResponse(json_data={"success": True}))
    api = DummyApi(requires_2fa=True, session=session)

    monkeypatch.setattr(icloud_service, "_sessions", {apple_id: api})
    monkeypatch.setattr(
        icloud_service,
        "_trusted_devices",
        {
            apple_id: [
                {"id": 7, "numberWithDialCode": "+49 777", "pushMode": "sms"},
            ]
        },
    )

    result = icloud_service.submit_2sa_code(apple_id, 0, "123456")

    assert result["status"] == "authenticated"
    assert api.trust_session_called is True

    method, url, headers, payload = session.calls[0]
    assert method == "post"
    assert url == f"{DummyApi.AUTH_ENDPOINT}/verify/phone/securitycode"
    assert headers["Accept"] == "application/json"
    assert payload["json"] == {
        "phoneNumber": {"id": 7},
        "securityCode": {"code": "123456"},
        "mode": "sms",
    }


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
