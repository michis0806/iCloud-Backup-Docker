"""Tests for backup progress tracking."""

from app.services.backup_service import get_progress, _set_progress, _clear_progress


class TestProgress:
    def test_no_progress_initially(self):
        assert get_progress(9999) is None

    def test_set_and_get(self):
        _set_progress(100, {"phase": "drive", "downloaded": 5})
        result = get_progress(100)
        assert result is not None
        assert result["phase"] == "drive"
        assert result["downloaded"] == 5
        _clear_progress(100)

    def test_clear(self):
        _set_progress(101, {"phase": "photos"})
        _clear_progress(101)
        assert get_progress(101) is None

    def test_clear_nonexistent(self):
        # Should not raise
        _clear_progress(99999)

    def test_overwrite(self):
        _set_progress(102, {"phase": "drive", "downloaded": 1})
        _set_progress(102, {"phase": "drive", "downloaded": 2})
        result = get_progress(102)
        assert result["downloaded"] == 2
        _clear_progress(102)
