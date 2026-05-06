"""AllocationPendingQueueRepository — DB-backed pending-allocation queue.

Rows here represent allocation requests that couldn't be fulfilled at
the time of the original ``plan_initial`` / ``plan_retry`` call (no
eligible target).  The dispatch daemon's per-tick re-evaluator iterates
these rows and feeds them back through the SAME ``AllocationPlanning
Service`` strategies — no duplicate allocation logic anywhere.  On
success the row is converted into a ``dispatch_queue`` entry and
deleted; on still-no-target it stays here with its
``last_attempted_at`` stamp updated.

Two row types via the ``type`` column (constants below):
  * ``TYPE_INITIAL_GROUP``  — whole-group initial plan re-attempt
  * ``TYPE_RETRY_CHUNK``    — single-chunk retry re-attempt

Composes ``PendingQueueRedisMirror`` so every successful write is
mirrored to Redis on a background executor.  Postgres remains source
of truth; Redis just accelerates the per-group read used by the
detail-page poller.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from serverV2.infrastructure.db import execute, execute_returning, query_all

if TYPE_CHECKING:
    from serverV2.allocation.pending_queue_redis_mirror import (
        PendingQueueRedisMirror,
    )

log = logging.getLogger(__name__)


# Row-type discriminator constants.  Used by the re-evaluator to pick
# the right planning method, and as the CHECK constraint value list on
# the ``type`` column.
TYPE_INITIAL_GROUP = "initial_group"
TYPE_RETRY_CHUNK = "retry_chunk"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AllocationPendingItem:
    type: str
    group_id: str
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    chunk_index: int | None = None
    attempt: int | None = None
    engine: str | None = None
    tier: str | None = None
    excluded_machine_ids: tuple[str, ...] = field(default_factory=tuple)
    excluded_serverless_capabilities: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    render_overrides_json: str = ""
    max_retries: int = 0
    priority: int = 0
    machine_ids: tuple[str, ...] = field(default_factory=tuple)
    input_filename: str = ""
    id: int | None = None        # populated when read back from DB
    created_at: str = ""
    last_attempted_at: str | None = None


class AllocationPendingQueueRepository:

    def __init__(self, mirror: "PendingQueueRedisMirror | None" = None) -> None:
        self._mirror = mirror

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def enqueue(self, item: AllocationPendingItem) -> None:
        item.created_at = _now_iso()
        row = execute_returning(
            """INSERT INTO pending_allocation_queue
               (type, group_id, chunk_index, attempt,
                frame_start, frame_end, frame_step, total_frames,
                engine, tier,
                excluded_machine_ids, excluded_serverless_capabilities,
                render_overrides_json, max_retries, priority,
                machine_ids, input_filename,
                created_at)
               VALUES (%s, %s, %s, %s,
                       %s, %s, %s, %s,
                       %s, %s,
                       %s::jsonb, %s::jsonb,
                       %s, %s, %s,
                       %s::jsonb, %s,
                       %s)
               RETURNING id""",
            (
                item.type, item.group_id, item.chunk_index, item.attempt,
                item.frame_start, item.frame_end, item.frame_step, item.total_frames,
                item.engine, item.tier,
                json.dumps(list(item.excluded_machine_ids)),
                json.dumps([list(p) for p in item.excluded_serverless_capabilities]),
                item.render_overrides_json, item.max_retries, item.priority,
                json.dumps(list(item.machine_ids)), item.input_filename,
                item.created_at,
            ),
        )
        item.id = int(row["id"])
        if self._mirror is not None:
            self._mirror.add(item)

    def update_last_attempted_at(self, row_id: int) -> None:
        ts = _now_iso()
        row = execute_returning(
            "UPDATE pending_allocation_queue SET last_attempted_at = %s "
            "WHERE id = %s RETURNING group_id",
            (ts, row_id),
        )
        if row and self._mirror is not None:
            self._mirror.update_last_attempted_at(row["group_id"], row_id, ts)

    def delete(self, row_id: int) -> None:
        row = execute_returning(
            "DELETE FROM pending_allocation_queue WHERE id = %s RETURNING group_id",
            (row_id,),
        )
        if row and self._mirror is not None:
            self._mirror.remove(row["group_id"], row_id)

    def delete_for_group(self, group_id: str) -> int:
        """Drop every pending row for ``group_id``.  Called by the cancel
        pipeline (via the dispatch queue service) so a cancelled group's
        parked rows can't be promoted on the next daemon tick.  Returns
        the count removed."""
        rows = query_all(
            "DELETE FROM pending_allocation_queue WHERE group_id = %s RETURNING id",
            (group_id,),
        )
        if self._mirror is not None:
            self._mirror.clear_for_group(group_id)
        return len(rows)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def list_all(self) -> list[AllocationPendingItem]:
        """All pending rows, ordered by priority DESC then created_at ASC.
        Used by the daemon's per-tick re-evaluator.
        """
        rows = query_all(
            """SELECT * FROM pending_allocation_queue
               ORDER BY priority DESC, created_at ASC""",
        )
        return [_row_to_item(r) for r in rows]

    def has_any(self) -> bool:
        """Cheap existence check used by the daemon's idle-tick guard.
        Returns True iff at least one pending row exists.
        """
        row = execute_returning(
            "SELECT 1 AS x FROM pending_allocation_queue LIMIT 1",
        )
        return row is not None

    def list_for_group(self, group_id: str) -> list[AllocationPendingItem]:
        """Per-group read used by the detail-page poller.  Tries Redis
        first; falls back to Postgres on a cold cache and rehydrates the
        cache for next time.
        """
        if self._mirror is not None:
            cached = self._mirror.get_for_group(group_id)
            if cached is not None:
                return cached
        rows = query_all(
            """SELECT * FROM pending_allocation_queue
               WHERE group_id = %s
               ORDER BY priority DESC, created_at ASC""",
            (group_id,),
        )
        items = [_row_to_item(r) for r in rows]
        if self._mirror is not None:
            self._mirror.set_for_group(group_id, items)
        return items


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _row_to_item(row: dict) -> AllocationPendingItem:
    return AllocationPendingItem(
        id=row.get("id"),
        type=row["type"],
        group_id=row["group_id"],
        chunk_index=row.get("chunk_index"),
        attempt=row.get("attempt"),
        frame_start=row["frame_start"],
        frame_end=row["frame_end"],
        frame_step=row["frame_step"],
        total_frames=row["total_frames"],
        engine=row.get("engine"),
        tier=row.get("tier"),
        excluded_machine_ids=tuple(row.get("excluded_machine_ids") or []),
        excluded_serverless_capabilities=tuple(
            tuple(pair) for pair in (row.get("excluded_serverless_capabilities") or [])
        ),
        render_overrides_json=row.get("render_overrides_json") or "",
        max_retries=int(row.get("max_retries") or 0),
        priority=int(row.get("priority") or 0),
        machine_ids=tuple(row.get("machine_ids") or []),
        input_filename=row.get("input_filename") or "",
        created_at=row.get("created_at") or "",
        last_attempted_at=row.get("last_attempted_at"),
    )
