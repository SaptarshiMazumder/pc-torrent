"""Thread-safe in-memory state store for active Modal jobs."""

from __future__ import annotations

import threading

MAX_HISTORY_ENTRIES = 50


class ModalInstanceRegistry:

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
        with self._lock:
            return [dict(v) for v in self._data.values()]

    def append_history(self, job_id: str, entry: dict) -> None:
        with self._lock:
            rec = self._data.setdefault(job_id, {})
            hist = rec.get("status_history", [])
            hist.append(entry)
            rec["status_history"] = hist[-MAX_HISTORY_ENTRIES:]
