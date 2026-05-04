"""AllocationFacade -- the only public symbol of the allocation module.

Combines the planning service and the dispatch-queue service so the
orchestrator gets one import surface.

Public entry points:
  * ``plan_initial`` -- pure compute, used by the pre-render cost
    preview.  No side effects.
  * ``submit_initial`` -- plan + (enqueue OR park-on-pending-queue) for
    a whole render group.  The only path that writes to
    ``dispatch_queue`` directly (bypassing pending).
  * ``park_retry`` -- park a single retry chunk on
    ``pending_allocation_queue``.  Both auto- and manual-retry use
    this; planning happens later inside the daemon's tick.
  * ``drain_for_group`` -- cancel-pipeline cleanup.

Constructed once at boot and shared.  Stateless beyond the injected
service references.
"""

from __future__ import annotations

from serverV2.allocation.allocation_pending_queue_repository import (
    AllocationPendingItem,
    TYPE_INITIAL_GROUP,
    TYPE_RETRY_CHUNK,
)
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.services.allocation_dispatch_queue_service import (
    AllocationDispatchQueueService,
)
from serverV2.allocation.services.allocation_planning_service import (
    AllocationPlanningService,
)
from serverV2.core.models import (
    AvailableResources,
    DispatchContext,
    PlannedTask,
    SubmitInitialResult,
)


class AllocationFacade:

    def __init__(
        self,
        *,
        planning: AllocationPlanningService,
        dispatch_queue: AllocationDispatchQueueService,
    ) -> None:
        self._planning = planning
        self._dispatch_queue = dispatch_queue

    def plan_initial(
        self,
        *,
        tier: str | None,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        engine: str | None = None,
        heaviness: dict | None = None,
        tier_budget_usd: float | None = None,
    ) -> list[PlannedTask]:
        return self._planning.plan_initial(
            tier=tier,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
            tier_budget_usd=tier_budget_usd,
        )

    def submit_initial(
        self,
        *,
        group_id: str,
        tier: str | None,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        engine: str | None,
        heaviness: dict | None,
        tier_budget_usd: float | None,
        machine_ids: list[str] | None,
        dispatch_context: DispatchContext,
    ) -> SubmitInitialResult:
        """Plan + enqueue a whole render group.  If the strategy can't
        find any eligible target right now, parks the request on
        ``pending_allocation_queue`` for the daemon to re-evaluate every
        tick.  Either way, the caller gets a single result back and
        never has to know which path fired."""
        planned = self._planning.plan_initial(
            tier=tier,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
            tier_budget_usd=tier_budget_usd,
        )
        if planned:
            dispatch_results = self._dispatch_queue.enqueue(
                group_id, planned, dispatch_context,
            )
            return SubmitInitialResult(
                planned=planned,
                dispatch_results=dispatch_results,
                parked=False,
            )
        # No eligible target — park.  The daemon's per-tick re-eval
        # will run plan_initial again with a fresher snapshot.
        file_size_bytes = (heaviness or {}).get("file_size_bytes")
        self._dispatch_queue.enqueue_pending(AllocationPendingItem(
            type=TYPE_INITIAL_GROUP,
            group_id=group_id,
            chunk_index=None,
            attempt=None,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            file_size_bytes=int(file_size_bytes) if file_size_bytes else None,
            engine=engine,
            tier=tier,
            machine_ids=tuple(machine_ids or ()),
            render_overrides_json=dispatch_context.render_overrides_json,
            max_retries=dispatch_context.max_retries,
            priority=dispatch_context.priority,
            input_filename=dispatch_context.input_filename,
        ))
        return SubmitInitialResult(planned=[], dispatch_results=[], parked=True)

    def park_retry(
        self,
        *,
        chunk_request: AllocationChunkRequest,
        tier: str | None,
        dispatch_context: DispatchContext,
    ) -> None:
        """Park a retry attempt onto ``pending_allocation_queue`` without
        any synchronous planning.  The dispatch daemon picks it up on its
        next tick, plans against its tick-local mutable snapshot, and
        either enqueues to ``dispatch_queue`` or leaves it parked.

        Both auto-retry callbacks (Vast/Modal monitor threads) and
        manual-retry endpoint flow through here.  No thread other than
        the daemon's tick reads the cache, so there is no read-after-
        mutate race possible.
        """
        self._dispatch_queue.enqueue_pending(AllocationPendingItem(
            type=TYPE_RETRY_CHUNK,
            group_id=chunk_request.group_id,
            chunk_index=chunk_request.chunk_index,
            attempt=chunk_request.attempt,
            frame_start=chunk_request.frame_start,
            frame_end=chunk_request.frame_end,
            frame_step=chunk_request.frame_step,
            total_frames=chunk_request.total_frames,
            file_size_bytes=chunk_request.file_size_bytes,
            engine=chunk_request.engine,
            tier=tier,
            excluded_machine_ids=chunk_request.excluded_machine_ids,
            excluded_serverless_capabilities=chunk_request.excluded_serverless_capabilities,
            render_overrides_json=dispatch_context.render_overrides_json,
            max_retries=dispatch_context.max_retries,
            priority=dispatch_context.priority,
            input_filename=dispatch_context.input_filename,
        ))

    def drain_for_group(self, group_id: str) -> int:
        """Remove every queued row (dispatch + pending) for ``group_id``.
        Cancel pipeline calls this through ``AllocationClient`` so it
        never imports the queue repos directly."""
        return self._dispatch_queue.drain_for_group(group_id)
