"""Tests for the ring buffer log handler."""

import logging
from app.services.log_handler import RingBufferHandler


class TestRingBufferHandler:
    def setup_method(self):
        self.handler = RingBufferHandler(maxlen=10)
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.getLogger("test-ring-buffer")
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def teardown_method(self):
        self.logger.removeHandler(self.handler)

    def test_captures_messages(self):
        self.logger.info("hello")
        entries = self.handler.get_entries()
        assert len(entries) == 1
        assert entries[0]["message"] == "hello"
        assert entries[0]["level"] == "INFO"

    def test_respects_maxlen(self):
        for i in range(20):
            self.logger.info(f"msg {i}")
        entries = self.handler.get_entries()
        assert len(entries) == 10
        assert entries[0]["message"] == "msg 10"

    def test_after_id_filter(self):
        self.logger.info("first")
        self.logger.info("second")
        entries = self.handler.get_entries()
        first_id = entries[0]["id"]

        self.logger.info("third")
        new_entries = self.handler.get_entries(after_id=first_id)
        messages = [e["message"] for e in new_entries]
        assert "first" not in messages
        assert "second" in messages
        assert "third" in messages

    def test_limit(self):
        for i in range(10):
            self.logger.info(f"msg {i}")
        entries = self.handler.get_entries(limit=3)
        assert len(entries) == 3

    def test_entry_structure(self):
        self.logger.warning("test warning")
        entry = self.handler.get_entries()[0]
        assert "id" in entry
        assert "timestamp" in entry
        assert entry["level"] == "WARNING"
        assert entry["message"] == "test warning"
