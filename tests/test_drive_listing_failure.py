"""A failed iCloud Drive listing must never reconcile local files as deleted."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import backup_service


class Folder:
    type = "folder"
    data = {}
    etag = "folder-etag"

    def __init__(self, children=None, error=None):
        self.children = children or {}
        self.error = error

    def dir(self):
        if self.error:
            raise self.error
        return list(self.children)

    def __getitem__(self, name):
        return self.children[name]


class File:
    type = "file"
    size = 10
    date_modified = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = {"drivewsid": "FILE::com.apple.CloudDocs::existing"}


@pytest.mark.parametrize("failure_location", ["root", "nested"])
def test_listing_failure_preserves_local_files_and_skips_reconciliation(
    tmp_path, monkeypatch, failure_location,
):
    local_root = tmp_path / "destination" / "drive"
    local_folder = local_root / "Home Assistant Backups"
    local_folder.mkdir(parents=True)
    existing = local_folder / "old-ha.tar"
    existing.write_bytes(b"keep this backup")

    listing_error = TimeoutError("iCloud Drive listing timed out")
    if failure_location == "root":
        root = Folder(error=listing_error)
    else:
        root = Folder({
            "already-synced.tar": File(),
            "Nested": Folder(error=listing_error),
        })

    api = SimpleNamespace(drive={"Home Assistant Backups": root})
    monkeypatch.setattr(backup_service.icloud_service, "get_session", lambda _id: api)
    monkeypatch.setattr(backup_service.settings, "backup_path", tmp_path)
    monkeypatch.setattr(backup_service, "_load_cache", lambda *_args: {"old": "etag"})
    monkeypatch.setattr(backup_service, "_file_needs_update", lambda *_args: False)
    apply_policy = Mock()
    save_cache = Mock()
    monkeypatch.setattr(backup_service, "_apply_sync_policy", apply_policy)
    monkeypatch.setattr(backup_service, "_save_cache", save_cache)

    with pytest.raises(TimeoutError, match="listing timed out"):
        backup_service.sync_drive_folder(
            "test@example.com",
            "Home Assistant Backups",
            local_root,
            "destination",
            sync_policy=backup_service.SyncPolicy.DELETE,
        )

    assert existing.read_bytes() == b"keep this backup"
    apply_policy.assert_not_called()
    save_cache.assert_not_called()
