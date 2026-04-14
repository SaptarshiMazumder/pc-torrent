"""Thread-safe in-memory store for live fleet instance snapshots.

One class, shared by all fleets.  Monitor threads write, API reads.
"""

from __future__ import annotations

import threading
from typing import Any

from serverV2.core.models import InstanceSnapshot

MAX_HISTORY_ENTRIES = 50


class InstanceRegistry:

    def __init__(self) -> None:
        self._data: dict[str, InstanceSnapshot] = {}
        self._lock = threading.Lock()

    def update(self, job_id: str, snapshot: InstanceSnapshot) -> None:
        with self._lock:
            existing = self._data.get(job_id)
            if existing and existing.status_history:
                snapshot.status_history = (
                    existing.status_history + snapshot.status_history
                )[-MAX_HISTORY_ENTRIES:]
            self._data[job_id] = snapshot

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._data.pop(job_id, None)

    def get_all(self) -> list[InstanceSnapshot]:
        with self._lock:
            return list(self._data.values())

    def get_by_job(self, job_id: str) -> InstanceSnapshot | None:
        with self._lock:
            return self._data.get(job_id)
