"""Tests for shared-folder download fallbacks."""

from types import SimpleNamespace

from app.services.backup_service import _candidate_document_ids, _download_with_share_context


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
