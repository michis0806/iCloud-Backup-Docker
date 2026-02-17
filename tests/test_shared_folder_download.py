"""Tests for shared-folder download fallbacks."""

from types import SimpleNamespace

from app.services.backup_service import (
    _candidate_document_ids,
    _derive_ckdatabase_url,
    _download_via_cloudkit,
    _download_with_share_context,
    _shared_zone,
)


class _DummyResponse:
    def __init__(self, ok=True, payload=None):
        self.ok = ok
        self.reason = "OK" if ok else "Not Found"
        self.status_code = 200 if ok else 404
        self._payload = payload or {}

    def json(self):
        return self._payload


class _DummySession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params, kwargs))
        if url.endswith("/download/by_id"):
            return _DummyResponse(
                ok=True,
                payload={"data_token": {"url": "https://download.example/file"}},
            )
        return _DummyResponse(ok=True)


def test_download_with_share_context_flattens_nested_share_id():
    session = _DummySession()
    connection = SimpleNamespace(
        params={"base": "1"},
        _document_root="https://docws.example",
        session=session,
    )

    share_id = {
        "shareName": "SHARE",
        "recordName": "RECORD",
        "zoneID": {
            "zoneName": "com.apple.CloudDocs",
            "ownerRecordName": "_owner",
        },
    }

    _download_with_share_context(
        connection,
        docwsid="DOC-1",
        zone="com.apple.CloudDocs",
        share_id=share_id,
        stream=True,
    )

    _, first_params, _ = session.calls[0]
    assert first_params["document_id"] == "DOC-1"
    assert first_params["shareName"] == "SHARE"
    assert first_params["recordName"] == "RECORD"
    assert first_params["zoneID.zoneName"] == "com.apple.CloudDocs"
    assert first_params["zoneID.ownerRecordName"] == "_owner"

    # Compatibility aliases for services that expect flattened names
    assert first_params["zoneName"] == "com.apple.CloudDocs"
    assert first_params["ownerRecordName"] == "_owner"


def test_candidate_document_ids_includes_shared_variants():
    node_data = {
        "docwsid": "DOC-UUID",
        "item_id": "ITEM-UUID",
        "drivewsid": "FILE_IN_SHARED_FOLDER::com.apple.CloudDocs::DOC-UUID",
        "unifiedToken": "UTOKEN-1",
    }
    fresh_data = {"item_id": "ITEM-UUID-2"}

    candidates = _candidate_document_ids(node_data, fresh_data)

    assert candidates[0] == "DOC-UUID"
    assert "ITEM-UUID" in candidates
    assert "FILE_IN_SHARED_FOLDER::com.apple.CloudDocs::DOC-UUID" in candidates
    assert "UTOKEN-1" in candidates
    assert "ITEM-UUID-2" in candidates


def test_candidate_document_ids_deduplicates_values():
    data = {
        "docwsid": "SAME-ID",
        "item_id": "SAME-ID",
        "drivewsid": "FILE_IN_SHARED_FOLDER::com.apple.CloudDocs::SAME-ID",
    }

    candidates = _candidate_document_ids(data)

    assert candidates.count("SAME-ID") == 1


# ---- _shared_zone tests ----

def test_shared_zone_with_owner_record_name():
    share_id = {
        "shareName": "SHARE-UUID",
        "recordName": "SHARE-UUID",
        "zoneID": {
            "zoneName": "com.apple.CloudDocs",
            "ownerRecordName": "_abc123",
        },
    }
    assert _shared_zone(share_id) == "com.apple.CloudDocs:_abc123"


def test_shared_zone_without_owner_returns_default():
    share_id = {
        "shareName": "SHARE-UUID",
        "zoneID": {"zoneName": "com.apple.CloudDocs"},
    }
    assert _shared_zone(share_id) == "com.apple.CloudDocs"


def test_shared_zone_with_string_returns_default():
    assert _shared_zone("some-string") == "com.apple.CloudDocs"


def test_shared_zone_with_none_returns_default():
    assert _shared_zone(None) == "com.apple.CloudDocs"


def test_shared_zone_respects_custom_default():
    assert _shared_zone(None, "custom.zone") == "custom.zone"


def test_shared_zone_uses_zone_name_from_share_id():
    share_id = {
        "zoneID": {
            "zoneName": "custom.zone",
            "ownerRecordName": "_owner",
        },
    }
    assert _shared_zone(share_id, "fallback") == "custom.zone:_owner"


# ---- owner-qualified zone in _download_with_share_context ----

def test_download_with_share_context_uses_owner_zone_in_url():
    """The download URL must contain the owner-qualified zone."""
    session = _DummySession()
    connection = SimpleNamespace(
        params={"base": "1"},
        _document_root="https://docws.example",
        session=session,
    )

    share_id = {
        "shareName": "SHARE",
        "recordName": "RECORD",
        "zoneID": {
            "zoneName": "com.apple.CloudDocs",
            "ownerRecordName": "_owner123",
        },
    }

    _download_with_share_context(
        connection,
        docwsid="DOC-1",
        zone="com.apple.CloudDocs",
        share_id=share_id,
        stream=True,
    )

    # The first call should go to the owner-qualified zone URL
    url, _, _ = session.calls[0]
    assert "com.apple.CloudDocs:_owner123" in url
    assert url == "https://docws.example/ws/com.apple.CloudDocs:_owner123/download/by_id"


# ---- _derive_ckdatabase_url tests ----

def test_derive_ckdatabase_url_from_drivews():
    url = _derive_ckdatabase_url("https://p171-drivews.icloud.com:443")
    assert url == "https://p171-ckdatabasews.icloud.com:443"


def test_derive_ckdatabase_url_from_docws():
    url = _derive_ckdatabase_url("https://p42-docws.icloud.com:443")
    assert url == "https://p42-ckdatabasews.icloud.com:443"


def test_derive_ckdatabase_url_no_port():
    url = _derive_ckdatabase_url("https://p1-drivews.icloud.com")
    assert url == "https://p1-ckdatabasews.icloud.com"


def test_derive_ckdatabase_url_invalid():
    assert _derive_ckdatabase_url("http://example.com") is None


# ---- _download_via_cloudkit tests ----

class _CKSession:
    """Session mock that simulates CloudKit records/lookup responses."""

    def __init__(self, lookup_response=None, download_response=None):
        self.calls = []
        self._lookup_response = lookup_response
        self._download_response = download_response or _DummyResponse(ok=True)

    def post(self, url, params=None, json=None, **kwargs):
        self.calls.append(("POST", url, params, json))
        if self._lookup_response:
            return self._lookup_response
        return _DummyResponse(ok=True, payload={"records": []})

    def get(self, url, params=None, **kwargs):
        self.calls.append(("GET", url, params, kwargs))
        return self._download_response


def test_cloudkit_download_calls_records_lookup():
    """CloudKit download should POST to records/lookup with correct zone."""
    ck_record = {
        "recordName": "DOC-UUID",
        "fields": {
            "fileContent": {
                "value": {"downloadURL": "https://cvws.example/download/file123"}
            }
        },
    }
    session = _CKSession(
        lookup_response=_DummyResponse(ok=True, payload={"records": [ck_record]}),
        download_response=_DummyResponse(ok=True),
    )
    connection = SimpleNamespace(
        params={"dsid": "123"},
        service_root="https://p171-drivews.icloud.com:443",
        session=session,
    )
    share_id = {
        "zoneID": {
            "zoneName": "com.apple.CloudDocs",
            "ownerRecordName": "_owner999",
        },
    }

    _download_via_cloudkit(connection, "DOC-UUID", share_id, stream=True)

    # First call: POST to records/lookup
    method, url, _, body = session.calls[0]
    assert method == "POST"
    assert "ckdatabasews" in url
    assert "/records/lookup" in url
    assert body["records"][0]["recordName"] == "DOC-UUID"
    assert body["zoneID"]["ownerRecordName"] == "_owner999"

    # Second call: GET the download URL
    method2, url2, _, _ = session.calls[1]
    assert method2 == "GET"
    assert url2 == "https://cvws.example/download/file123"


def test_cloudkit_download_no_owner_raises():
    """CloudKit download should raise if shareID has no ownerRecordName."""
    import pytest

    session = _CKSession()
    connection = SimpleNamespace(
        params={},
        service_root="https://p1-drivews.icloud.com:443",
        session=session,
    )
    share_id = {"zoneID": {"zoneName": "com.apple.CloudDocs"}}

    with pytest.raises(ValueError, match="ownerRecordName"):
        _download_via_cloudkit(connection, "DOC-1", share_id)
