"""DispatchQueueRepository — DB-backed dispatch queue.

Each queue row carries everything needed to dispatch the chunk later when
a slot opens up in its target fleet — frame info plus the per-render
DispatchContext (input filename, overrides, max retries, priority) plus
the chosen target (fleet + gpu_type for serverless, fleet + machine_id
for community).

After Phase 2 of the allocator redesign, the coordinator may leave items
sitting in this queue across the fleet-cap boundary; success/failure
handlers drain them as containers complete.
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
    fleet: str
    label: str = ""
    vram_gb: float = 0.0
    render_speed: float = 1.0
    gpu_type: str | None = None
    machine_id: str | None = None
    input_filename: str = ""
    render_overrides_b64: str = ""
    max_retries: int = 0
    priority: int = 0
    attempt: int = 0
    chunk_index: int | None = None
    group_id: str = ""    # populated when read back from DB


class DispatchQueueRepository:

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def enqueue(self, group_id: str, item: QueueItem) -> None:
        execute(
            """INSERT INTO dispatch_queue
               (group_id, frame_start, frame_end, frame_step,
                total_frames, attempt, chunk_index,
                fleet, gpu_type, machine_id,
                input_filename, render_overrides_b64,
                max_retries, priority,
                created_at)
               VALUES (%s, %s, %s, %s,
                       %s, %s, %s,
                       %s, %s, %s,
                       %s, %s,
                       %s, %s,
                       %s)""",
            (
                group_id, item.frame_start, item.frame_end, item.frame_step,
                item.total_frames, item.attempt, item.chunk_index,
                item.fleet, item.gpu_type, item.machine_id,
                item.input_filename, item.render_overrides_b64,
                item.max_retries, item.priority,
                _now_iso(),
            ),
        )

    def enqueue_all(self, group_id: str, items: list[QueueItem]) -> None:
        for item in items:
            self.enqueue(group_id, item)

    # ------------------------------------------------------------------
    # reads + pops
    # ------------------------------------------------------------------

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
        return _row_to_item(row) if row else None

    def dequeue_for_fleet(self, fleet: str) -> QueueItem | None:
        """Pop the oldest queued item targeting ``fleet``, across all groups.
        Used by drain after a slot opens up in that fleet.
        """
        row = execute_returning(
            """DELETE FROM dispatch_queue
               WHERE id = (
                   SELECT id FROM dispatch_queue
                   WHERE fleet = %s
                   ORDER BY priority DESC, id ASC
                   LIMIT 1
               )
               RETURNING *""",
            (fleet,),
        )
        return _row_to_item(row) if row else None

    def count_for_fleet(self, fleet: str) -> int:
        row = execute_returning(
            "SELECT COUNT(*) AS cnt FROM dispatch_queue WHERE fleet = %s",
            (fleet,),
        )
        return row["cnt"] if row else 0

    def count(self, group_id: str) -> int:
        row = execute_returning(
            "SELECT COUNT(*) AS cnt FROM dispatch_queue WHERE group_id = %s",
            (group_id,),
        )
        return row["cnt"] if row else 0

    def drain(self, group_id: str) -> int:
        """Remove all queued items for a group. Returns count removed."""
        rows = query_all(
            "DELETE FROM dispatch_queue WHERE group_id = %s RETURNING id",
            (group_id,),
        )
        return len(rows)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _row_to_item(row: dict) -> QueueItem:
    return QueueItem(
        frame_start=row["frame_start"],
        frame_end=row["frame_end"],
        frame_step=row["frame_step"],
        total_frames=row["total_frames"],
        fleet=row.get("fleet") or "",
        label="",
        vram_gb=0.0,
        render_speed=1.0,
        gpu_type=row.get("gpu_type"),
        machine_id=row.get("machine_id"),
        input_filename=row.get("input_filename") or "",
        render_overrides_b64=row.get("render_overrides_b64") or "",
        max_retries=int(row.get("max_retries") or 0),
        priority=int(row.get("priority") or 0),
        attempt=int(row.get("attempt") or 0),
        chunk_index=row.get("chunk_index"),
        group_id=row.get("group_id") or "",
    )
