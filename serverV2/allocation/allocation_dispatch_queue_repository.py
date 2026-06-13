"""AllocationDispatchQueueRepository — DB-backed dispatch queue.

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
class AllocationQueueItem:
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    fleet: str
    chunk_index: int
    label: str = ""
    vram_gb: float = 0.0
    render_speed: float = 1.0
    gpu_type: str | None = None
    machine_id: str | None = None
    input_filename: str = ""
    render_overrides_json: str = ""
    max_retries: int = 0
    priority: int = 0
    attempt: int = 0
    group_id: str = ""    # populated when read back from DB
    # Pre-generated at enqueue time so the caller can synthesize a
    # DispatchResult immediately (preserving the orchestrator's
    # synchronous start_render contract).  The same job_id is reused
    # at actual dispatch time.
    job_id: str = ""
    # Per-chunk estimates carried through from AllocationPlanner so the
    # dispatch handler can stamp them onto the jobs row without
    # re-computing.
    price_per_hour: float = 0.0
    estimated_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    estimated_seconds_per_frame: float = 0.0
    estimated_startup_seconds: float = 0.0
    # Per-offer fields for Vast (None for Modal/community).  offer_id is
    # the rentable bundle id chosen at planning time; cuda_version /
    # host_os are captured for telemetry + post-dispatch logging.
    offer_id: int | None = None
    cuda_version: str | None = None
    host_os: str | None = None


class AllocationDispatchQueueRepository:

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def enqueue(self, group_id: str, item: AllocationQueueItem) -> None:
        # ON CONFLICT DO NOTHING is the DB-level safety net for the
        # duplicate-dispatch race: the coordinator's app-level check
        # against in_progress_chunks already filters most duplicates,
        # but if two enqueues somehow race past it, the UNIQUE
        # constraint on (group_id, chunk_index) keeps the queue at
        # exactly one row per chunk.
        execute(
            """INSERT INTO dispatch_queue
               (group_id, frame_start, frame_end, frame_step,
                total_frames, attempt, chunk_index,
                fleet, gpu_type, machine_id,
                input_filename, render_overrides_json,
                max_retries, priority,
                job_id,
                price_per_hour,
                estimated_seconds, estimated_cost_usd,
                estimated_seconds_per_frame, estimated_startup_seconds,
                offer_id, cuda_version, host_os,
                created_at)
               VALUES (%s, %s, %s, %s,
                       %s, %s, %s,
                       %s, %s, %s,
                       %s, %s,
                       %s, %s,
                       %s,
                       %s,
                       %s, %s,
                       %s, %s,
                       %s, %s, %s,
                       %s)
               ON CONFLICT (group_id, chunk_index) DO NOTHING""",
            (
                group_id, item.frame_start, item.frame_end, item.frame_step,
                item.total_frames, item.attempt, item.chunk_index,
                item.fleet, item.gpu_type, item.machine_id,
                item.input_filename, item.render_overrides_json,
                item.max_retries, item.priority,
                item.job_id,
                item.price_per_hour,
                item.estimated_seconds, item.estimated_cost_usd,
                item.estimated_seconds_per_frame, item.estimated_startup_seconds,
                item.offer_id, item.cuda_version, item.host_os,
                _now_iso(),
            ),
        )

    def enqueue_all(self, group_id: str, items: list[AllocationQueueItem]) -> None:
        for item in items:
            self.enqueue(group_id, item)

    # ------------------------------------------------------------------
    # reads + pops
    # ------------------------------------------------------------------

    def dequeue(self, group_id: str) -> AllocationQueueItem | None:
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

    def dequeue_for_fleet(self, fleet: str) -> AllocationQueueItem | None:
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

    def has_any_for_fleets(self, fleets: list[str]) -> bool:
        """Cheap existence check: is anything queued for any of the
        given fleets?  ``LIMIT 1`` so the planner stops at the first
        match — used by the dispatch daemon's per-tick early-return so
        idle ticks don't touch Redis or the fleet-availability builders.
        """
        if not fleets:
            return False
        row = execute_returning(
            "SELECT 1 AS x FROM dispatch_queue WHERE fleet = ANY(%s) LIMIT 1",
            (list(fleets),),
        )
        return row is not None

    def count_by_priority_and_fleet(self) -> dict[str, dict[str, dict[str, int]]]:
        """Job + frame counts grouped by fleet + priority level.

        Reads the dispatch queue (items planned but not yet sent to a
        provider).  Used together with the pending-queue counts to
        populate the per-fleet queue-depth chip shown in the UI.

        Returned shape::

            {fleet: {priority_level: {"jobs": N, "frames": F}}}
        """
        rows = query_all(
            """SELECT fleet, priority,
                      COUNT(*) AS jobs,
                      COALESCE(SUM(total_frames), 0) AS frames
               FROM dispatch_queue
               GROUP BY fleet, priority""",
        )
        out: dict[str, dict[str, dict[str, int]]] = {
            f: {l: {"jobs": 0, "frames": 0}
                for l in ("low", "normal", "high")}
            for f in ("vast", "modal", "community")
        }
        for r in rows:
            fleet = _normalize_fleet_label(str(r.get("fleet") or ""))
            if fleet not in out:
                continue
            level = _priority_level_label(int(r.get("priority") or 0))
            out[fleet][level]["jobs"] += int(r.get("jobs") or 0)
            out[fleet][level]["frames"] += int(r.get("frames") or 0)
        return out

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

def _priority_level_label(priority: int) -> str:
    """Map a numeric priority into the queue-depth bucket label."""
    if priority <= 0:
        return "low"
    if priority >= 2:
        return "high"
    return "normal"


def _normalize_fleet_label(fleet: str) -> str:
    """Map the row's fleet string into the queue-depth bucket name.

    ``machine_type`` values in the dispatch queue follow the live names
    (``vast_serverless`` / ``modal_serverless`` / ``windows`` for
    community).  The UI surface uses short labels.
    """
    if fleet.startswith("vast"):
        return "vast"
    if fleet.startswith("modal"):
        return "modal"
    return "community"


def _row_to_item(row: dict) -> AllocationQueueItem:
    offer_raw = row.get("offer_id")
    offer_id = int(offer_raw) if offer_raw is not None else None
    return AllocationQueueItem(
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
        render_overrides_json=row.get("render_overrides_json") or "",
        max_retries=int(row.get("max_retries") or 0),
        priority=int(row.get("priority") or 0),
        attempt=int(row.get("attempt") or 0),
        chunk_index=row.get("chunk_index"),
        group_id=row.get("group_id") or "",
        job_id=row.get("job_id") or "",
        price_per_hour=float(row.get("price_per_hour") or 0.0),
        estimated_seconds=float(row.get("estimated_seconds") or 0.0),
        estimated_cost_usd=float(row.get("estimated_cost_usd") or 0.0),
        estimated_seconds_per_frame=float(row.get("estimated_seconds_per_frame") or 0.0),
        estimated_startup_seconds=float(row.get("estimated_startup_seconds") or 0.0),
        offer_id=offer_id,
        cuda_version=row.get("cuda_version"),
        host_os=row.get("host_os"),
    )
