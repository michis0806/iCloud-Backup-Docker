"""Tests for shared-folder download fallbacks."""

from types import SimpleNamespace

from app.services.backup_service import (
    _candidate_document_ids,
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


# ---- pyicloud 2026.10.0 owner-addressed downloads ---------------------------

from unittest.mock import Mock  # noqa: E402

import pytest  # noqa: E402
from pyicloud.exceptions import PyiCloudAPIResponseException  # noqa: E402
from pyicloud.services.drive import DriveNode, DriveService  # noqa: E402

from app.services.backup_service import _open_drive_node  # noqa: E402

_SHARE = {
    "shareName": "SHARE",
    "recordName": "RECORD",
    "zoneID": {"zoneName": "com.apple.CloudDocs", "ownerRecordName": "_owner"},
}


def _shared_data(**extra):
    data = {
        "docwsid": "DOC-1",
        "drivewsid": "FILE_IN_SHARED_FOLDER::com.apple.CloudDocs::DOC-1",
        "zone": "com.apple.CloudDocs",
        "shareID": _SHARE,
        "size": 3,
    }
    data.update(extra)
    return data


class _OwnerPathSession(_DummySession):
    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params, kwargs))
        if "/download/by_id" in url:
            return _DummyResponse(payload={"data_token": {"url": "https://download.example/file"}})
        return _DummyResponse(ok=True)


def _drive_service(session):
    return DriveService("https://drivews.example", "https://docws.example", session, {"dsid": "1"})


def test_upstream_open_uses_owner_path_when_node_has_owner():
    session = _OwnerPathSession()
    node = DriveNode(_drive_service(session), _shared_data(owner={"ownerRecordName": "_owner"}))
    node.open(stream=True)
    url, params, kwargs = session.calls[0]
    assert url == "https://docws.example/ws/com.apple.CloudDocs/download/by_id/_owner"
    assert params["document_id"] == "DOC-1"
    assert "owner_record_name" not in kwargs


def test_upstream_open_without_owner_dict_uses_plain_path():
    session = _OwnerPathSession()
    node = DriveNode(_drive_service(session), _shared_data())
    node.open(stream=True)
    assert session.calls[0][0] == "https://docws.example/ws/com.apple.CloudDocs/download/by_id"


def _failing_node(data):
    node = Mock()
    node.data = data
    node.open.side_effect = PyiCloudAPIResponseException("Not Found", 404)
    node.connection.get_file.return_value = "RESPONSE"
    return node


def test_fallback_tries_owner_path_from_share_id_first():
    node = _failing_node(_shared_data())
    assert _open_drive_node(node, "Shared/a.txt", stream=True) == "RESPONSE"
    node.connection.get_file.assert_called_once_with(
        "DOC-1", zone="com.apple.CloudDocs", owner_record_name="_owner", stream=True,
    )


def test_fallback_skips_owner_path_already_tried_by_pyicloud():
    node = _failing_node(_shared_data(owner={"ownerRecordName": "_owner"}))
    assert _open_drive_node(node, "Shared/a.txt", stream=True) == "RESPONSE"
    # Next fallback: owner-qualified zone, our pre-2026.10 workaround.
    node.connection.get_file.assert_called_once_with(
        "DOC-1", zone="com.apple.CloudDocs:_owner", stream=True,
    )


def test_fallback_continues_to_owner_zone_when_owner_path_fails():
    node = _failing_node(_shared_data())
    node.connection.get_file.side_effect = [
        PyiCloudAPIResponseException("Not Found", 404), "RESPONSE",
    ]
    assert _open_drive_node(node, "Shared/a.txt", stream=True) == "RESPONSE"
    second = node.connection.get_file.call_args_list[1]
    assert second.kwargs["zone"] == "com.apple.CloudDocs:_owner"
    assert "owner_record_name" not in second.kwargs


def test_non_shared_file_has_no_owner_fallback():
    node = _failing_node({"docwsid": "DOC-1", "drivewsid": "FILE::com.apple.CloudDocs::DOC-1",
                          "zone": "com.apple.CloudDocs"})
    node.connection.session.post.side_effect = RuntimeError("offline")
    node.connection.get_file.side_effect = PyiCloudAPIResponseException("Not Found", 404)
    with pytest.raises(PyiCloudAPIResponseException):
        _open_drive_node(node, "a.txt", stream=True)
    for call in node.connection.get_file.call_args_list:
        assert "owner_record_name" not in call.kwargs
