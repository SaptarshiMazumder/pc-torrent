"""Dispatch queue — per-group queue of frame ranges awaiting dispatch.

Pure data container.  No dispatch logic, no I/O.  The RenderOrchestrator
owns flushing and machine-picking.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field


@dataclass
class QueueItem:
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    attempt: int = 0
    chunk_index: int | None = None


class GroupDispatchQueue:
    """Thread-safe FIFO of frame ranges for one render group."""

    def __init__(self, group_id: str, max_retries: int) -> None:
        self.group_id = group_id
        self.max_retries = max_retries
        self._queue: deque[QueueItem] = deque()
        self._lock = threading.Lock()

    def enqueue(self, item: QueueItem) -> None:
        with self._lock:
            self._queue.append(item)

    def enqueue_all(self, items: list[QueueItem]) -> None:
        with self._lock:
            self._queue.extend(items)

    def dequeue(self) -> QueueItem | None:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    def drain(self) -> list[QueueItem]:
        """Remove and return all pending items (used on cancellation)."""
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
            return items

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return len(self._queue) == 0


class DispatchQueueManager:
    """Holds active dispatch queues keyed by group_id."""

    def __init__(self) -> None:
        self._queues: dict[str, GroupDispatchQueue] = {}
        self._lock = threading.Lock()

    def create(self, group_id: str, max_retries: int) -> GroupDispatchQueue:
        queue = GroupDispatchQueue(group_id, max_retries)
        with self._lock:
            self._queues[group_id] = queue
        return queue

    def get(self, group_id: str) -> GroupDispatchQueue | None:
        with self._lock:
            return self._queues.get(group_id)

    def remove(self, group_id: str) -> None:
        with self._lock:
            self._queues.pop(group_id, None)
