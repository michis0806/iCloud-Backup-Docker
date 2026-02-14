"""In-memory ring buffer log handler for the web UI log viewer."""

import logging
import threading
from collections import deque
from datetime import datetime


class RingBufferHandler(logging.Handler):
    """Stores the last *maxlen* log records in memory for retrieval via API."""

    def __init__(self, maxlen: int = 1000):
        super().__init__()
        self._buffer: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counter = 0

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "id": self._next_id(),
            "timestamp": datetime.utcfromtimestamp(record.created).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        }
        with self._lock:
            self._buffer.append(entry)

    def _next_id(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def get_entries(self, after_id: int = 0, limit: int = 200) -> list[dict]:
        """Return log entries with id > *after_id*, up to *limit* entries."""
        with self._lock:
            entries = [e for e in self._buffer if e["id"] > after_id]
        return entries[-limit:]


# Singleton instance
log_buffer = RingBufferHandler(maxlen=2000)
log_buffer.setFormatter(logging.Formatter("%(message)s"))
