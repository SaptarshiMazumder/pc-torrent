"""AllocationPendingReEvaluator — per-tick pass over the pending queue.

Pure orchestration.  Pulls each pending row, hands it to the existing
``AllocationPlanningService`` (the same strategies fresh allocations
use), and on success writes the resulting tasks to ``dispatch_queue``
+ deletes the pending row.  On still-no-target, stamps
``last_attempted_at`` so we can see whether re-eval is making
progress.

No allocation logic lives here — this is a loop, not a planner.
"""

from __future__ import annotations

import logging

from serverV2.allocation.allocation_pending_queue_repository import (
    AllocationPendingItem,
    AllocationPendingQueueRepository,
    TYPE_INITIAL_GROUP,
    TYPE_RETRY_CHUNK,
)
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_enqueue_handler import (
    AllocationEnqueueHandler,
)
from serverV2.allocation.services.allocation_planning_service import (
    AllocationPlanningService,
)
from serverV2.core.models import (
    AvailableResources,
    DispatchContext,
    PlannedTask,
)
from serverV2.fleets.fleet_availability.mutable_fleet_availability_snapshot import (
    MutableFleetAvailabilitySnapshot,
)

log = logging.getLogger(__name__)


class AllocationPendingReEvaluator:

    def __init__(
        self,
        *,
        pending_repo: AllocationPendingQueueRepository,
        planning_service: AllocationPlanningService,
        enqueue_handler: AllocationEnqueueHandler,
    ) -> None:
        self._pending_repo = pending_repo
        self._planning = planning_service
        self._enqueue = enqueue_handler

    def has_any(self) -> bool:
        return self._pending_repo.has_any()

    def re_evaluate(self, snapshot: MutableFleetAvailabilitySnapshot) -> int:
        """One pass over the pending queue.  Returns the number of rows
        promoted to ``dispatch_queue``.
        """
        moved = 0
        rows = self._pending_repo.list_all()
        for row in rows:
            try:
                if self._try_promote(row, snapshot):
                    moved += 1
                else:
                    self._pending_repo.update_last_attempted_at(row.id)  # type: ignore[arg-type]
            except Exception as exc:
                log.error(
                    "AllocationPendingReEvaluator: row %s (type=%s, group=%s) raised: %r",
                    row.id, row.type, row.group_id, exc,
                )
        if moved:
            log.info(
                "AllocationPendingReEvaluator: promoted %d row(s) from pending to dispatch_queue",
                moved,
            )
        return moved

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _try_promote(
        self,
        row: AllocationPendingItem,
        snapshot: MutableFleetAvailabilitySnapshot,
    ) -> bool:
        if row.type == TYPE_RETRY_CHUNK:
            tasks = self._plan_for_retry(row, snapshot)
        elif row.type == TYPE_INITIAL_GROUP:
            tasks = self._plan_for_initial(row, snapshot)
        else:
            log.warning(
                "AllocationPendingReEvaluator: unknown row type=%r (id=%s) — skipping",
                row.type, row.id,
            )
            return False

        if not tasks:
            return False

        ctx = DispatchContext(
            group_id=row.group_id,
            input_filename=row.input_filename,
            render_overrides_json=row.render_overrides_json,
            blend_url="",
            max_retries=row.max_retries,
            priority=row.priority,
            engine=row.engine,
        )
        # force_retry stays False here.  A pending row that came from a
        # manual retry never lands here in the first place — manual
        # retry raises rather than escalating to pending.
        self._enqueue.enqueue(row.group_id, tasks, ctx)
        self._pending_repo.delete(row.id)  # type: ignore[arg-type]
        return True

    def _plan_for_retry(
        self,
        row: AllocationPendingItem,
        snapshot: MutableFleetAvailabilitySnapshot,
    ) -> list[PlannedTask]:
        chunk_request = AllocationChunkRequest(
            group_id=row.group_id,
            chunk_index=row.chunk_index or 0,
            frame_start=row.frame_start,
            frame_end=row.frame_end,
            frame_step=row.frame_step,
            total_frames=row.total_frames,
            attempt=row.attempt or 0,
            excluded_machine_ids=row.excluded_machine_ids,
            excluded_serverless_capabilities=row.excluded_serverless_capabilities,
            file_size_bytes=row.file_size_bytes,
            engine=row.engine,
        )
        resources = _adapt_resources(snapshot, machine_ids=None)
        task = self._planning.plan_retry(
            tier=row.tier,
            chunk_request=chunk_request,
            resources=resources,
        )
        return [task] if task is not None else []

    def _plan_for_initial(
        self,
        row: AllocationPendingItem,
        snapshot: MutableFleetAvailabilitySnapshot,
    ) -> list[PlannedTask]:
        heaviness: dict | None
        if row.file_size_bytes is not None:
            heaviness = {"file_size_bytes": row.file_size_bytes}
        else:
            heaviness = None
        resources = _adapt_resources(
            snapshot,
            machine_ids=list(row.machine_ids) if row.machine_ids else None,
        )
        return self._planning.plan_initial(
            tier=row.tier,
            frame_start=row.frame_start,
            frame_end=row.frame_end,
            frame_step=row.frame_step,
            total_frames=row.total_frames,
            resources=resources,
            engine=row.engine,
            heaviness=heaviness,
        )


def _adapt_resources(
    snapshot: MutableFleetAvailabilitySnapshot,
    *,
    machine_ids: list[str] | None,
) -> AvailableResources:
    """Mirror of ``AllocationClient._adapt_resources`` — the strategies
    always consume an ``AvailableResources``, so the re-evaluator
    constructs one from the in-tick mutable snapshot it received.
    """
    community = list(snapshot.community_available)
    if machine_ids:
        wanted = set(machine_ids)
        return AvailableResources(
            community_machines=[m for m in community if m.id in wanted],
            serverless_capabilities=[],
            serverless_in_flight=dict(snapshot.serverless_in_flight),
        )
    return AvailableResources(
        community_machines=community,
        serverless_capabilities=(
            list(snapshot.vast_available) + list(snapshot.modal_available)
        ),
        serverless_in_flight=dict(snapshot.serverless_in_flight),
    )
