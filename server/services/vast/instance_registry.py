"""
InstanceRegistry — thread-safe in-memory state store for active Vast.ai instances.

The polling threads write into this registry; the REST API reads from it to
power the desktop live-monitoring UI.
"""

from __future__ import annotations

import threading


MAX_LOG_LINES = 200
MAX_HISTORY_ENTRIES = 50


class InstanceRegistry:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()

    def set(self, job_id: str, updates: dict) -> None:
        with self._lock:
            entry = self._data.setdefault(job_id, {})
            entry.update(updates)

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._data.pop(job_id, None)

    def get_all(self) -> list[dict]:
        """Return a snapshot of all currently tracked instance states."""
        with self._lock:
            return [dict(v) for v in self._data.values()]

    def append_history(self, job_id: str, entry: dict) -> None:
        with self._lock:
            rec = self._data.setdefault(job_id, {})
            hist = rec.get("status_history", [])
            hist.append(entry)
            rec["status_history"] = hist[-MAX_HISTORY_ENTRIES:]
