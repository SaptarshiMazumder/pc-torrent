"""AllocationFacade -- the only public symbol of the allocation module.

Combines the planning service and the dispatch-queue service so the
orchestrator gets one import surface.

Public entry points:
  * ``plan_initial`` -- pure compute, used by the pre-render cost
    preview.  No side effects.
  * ``submit_initial`` -- park a whole render group on
    ``pending_allocation_queue``.  Daemon plans + enqueues on its next
    tick.
  * ``park_retry`` -- park a single retry chunk on
    ``pending_allocation_queue``.  Both auto- and manual-retry use
    this; planning happens later inside the daemon's tick.
  * ``drain_for_group`` -- cancel-pipeline cleanup.

Architectural invariant: NO public method here writes to
``dispatch_queue``.  All allocation entry points park to
``pending_allocation_queue``; the dispatch daemon is the sole writer
of ``dispatch_queue`` (via its tick-local mutable snapshot).  This
removes every cross-thread race between planning and dispatching that
caused dogpile bugs in the old design.

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
        engine: str | None,
        heaviness: dict | None,
        machine_ids: list[str] | None,
        dispatch_context: DispatchContext,
    ) -> None:
        """Park a whole render group on ``pending_allocation_queue``.
        The daemon plans + enqueues against its tick-local mutable
        snapshot on its next tick.  Returns nothing -- caller acks the
        submission, then polls ``GET /render-groups/{id}`` for live
        chunk state as the daemon dispatches.
        """
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
