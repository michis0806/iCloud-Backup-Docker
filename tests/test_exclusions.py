"""Tests for exclusion pattern matching."""

import pytest
from app.services.backup_service import is_excluded


class TestGlobPatterns:
    def test_star_extension(self):
        assert is_excluded("Documents/notes.tmp", ["*.tmp"])

    def test_star_no_match(self):
        assert not is_excluded("Documents/notes.txt", ["*.tmp"])

    def test_dotfile_glob(self):
        assert is_excluded("project/.git/config", [".git"])

    def test_question_mark(self):
        assert is_excluded("file?.txt", ["file?.txt"])

    def test_bracket_pattern(self):
        assert is_excluded("log1.txt", ["log[0-9].txt"])


class TestSimpleNamePatterns:
    def test_name_in_path(self):
        assert is_excluded("src/node_modules/package.json", ["node_modules"])

    def test_name_at_root(self):
        assert is_excluded("node_modules/something", ["node_modules"])

    def test_name_not_substring(self):
        """A simple pattern should match whole path components, not substrings."""
        assert not is_excluded("my_node_modules_backup/file.txt", ["node_modules"])

    def test_ds_store(self):
        assert is_excluded("Photos/.DS_Store", [".DS_Store"])


class TestAbsolutePathPatterns:
    def test_exact_match(self):
        assert is_excluded("Documents/Projects", ["Documents/Projects"])

    def test_prefix_match(self):
        assert is_excluded("Documents/Projects/secret/file.txt", ["Documents/Projects"])

    def test_no_partial_match(self):
        assert not is_excluded("MyDocuments/Projects", ["Documents/Projects"])


class TestEmptyExclusions:
    def test_no_excludes(self):
        assert not is_excluded("anything.txt", [])

    def test_none_like_empty(self):
        assert not is_excluded("anything.txt", [])


class TestCombinedPatterns:
    def test_multiple_patterns(self):
        excludes = [".DS_Store", "*.tmp", "node_modules", "Documents/Temp"]
        assert is_excluded("foo/.DS_Store", excludes)
        assert is_excluded("bar/cache.tmp", excludes)
        assert is_excluded("project/node_modules/pkg", excludes)
        assert is_excluded("Documents/Temp/scratch.txt", excludes)
        assert not is_excluded("Documents/Important/file.pdf", excludes)
