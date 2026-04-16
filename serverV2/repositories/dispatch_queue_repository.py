"""DispatchQueueRepository — DB-backed dispatch queue for frame ranges.

Replaces the in-memory DispatchQueueManager. All state lives in PostgreSQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from serverV2.infrastructure.db import execute, execute_returning, query_all

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class QueueItem:
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    attempt: int = 0
    chunk_index: int | None = None


class DispatchQueueRepository:

    def enqueue(self, group_id: str, item: QueueItem) -> None:
        execute(
            """INSERT INTO dispatch_queue
               (group_id, frame_start, frame_end, frame_step,
                total_frames, attempt, chunk_index, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (group_id, item.frame_start, item.frame_end, item.frame_step,
             item.total_frames, item.attempt, item.chunk_index, _now_iso()),
        )

    def enqueue_all(self, group_id: str, items: list[QueueItem]) -> None:
        for item in items:
            self.enqueue(group_id, item)

    def dequeue(self, group_id: str) -> QueueItem | None:
        row = execute_returning(
            """DELETE FROM dispatch_queue
               WHERE id = (
                   SELECT id FROM dispatch_queue
                   WHERE group_id = %s
                   ORDER BY id ASC
                   LIMIT 1
               )
               RETURNING *""",
            (group_id,),
        )
        if not row:
            return None
        return QueueItem(
            frame_start=row["frame_start"],
            frame_end=row["frame_end"],
            frame_step=row["frame_step"],
            total_frames=row["total_frames"],
            attempt=row["attempt"],
            chunk_index=row.get("chunk_index"),
        )

    def dequeue_all(self, group_id: str) -> list[QueueItem]:
        rows = query_all(
            """DELETE FROM dispatch_queue
               WHERE group_id = %s
               RETURNING *""",
            (group_id,),
        )
        return [
            QueueItem(
                frame_start=r["frame_start"],
                frame_end=r["frame_end"],
                frame_step=r["frame_step"],
                total_frames=r["total_frames"],
                attempt=r["attempt"],
                chunk_index=r.get("chunk_index"),
            )
            for r in rows
        ]

    def drain(self, group_id: str) -> int:
        """Remove all queued items for a group. Returns count removed."""
        rows = query_all(
            "DELETE FROM dispatch_queue WHERE group_id = %s RETURNING id",
            (group_id,),
        )
        return len(rows)

    def count(self, group_id: str) -> int:
        row = execute_returning(
            "SELECT COUNT(*) AS cnt FROM dispatch_queue WHERE group_id = %s",
            (group_id,),
        )
        return row["cnt"] if row else 0
