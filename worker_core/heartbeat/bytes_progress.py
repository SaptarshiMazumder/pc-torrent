"""BytesProgress -- thread-safe byte counter for download/extract phases.

The download path increments this as bytes hit disk; the heartbeat
sender reads it for the bytes_progressed payload field.  Server-side
stall detection compares successive readings: counter unchanged for
DOWNLOAD_BYTES_STALL_SEC means the connection is alive but no data
is flowing -- distinct from a heartbeat-dead failure mode.
"""

from __future__ import annotations

import threading


class BytesProgress:

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        with self._lock:
            self._count += n

    def reset(self) -> None:
        with self._lock:
            self._count = 0

    def get(self) -> int:
        with self._lock:
            return self._count
