"""BytesProgress -- thread-safe byte counter for the agent's download phase.

The agent's download path increments this as bytes hit disk; the
heartbeat thread reads it for the bytes_progressed payload field.
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
