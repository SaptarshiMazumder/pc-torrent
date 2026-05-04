"""AllocationFacade — the only public symbol of the allocation module.

Three thin delegations to the two services.  No business logic here —
the facade exists to give orchestrator a single import surface and to
keep the services themselves opaque.

Constructed once at boot and shared.  Stateless beyond the injected
service references.
"""

from __future__ import annotations

from serverV2.allocation.allocation_pending_queue_repository import (
    AllocationPendingItem,
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
    DispatchResult,
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

    def plan_retry(
        self,
        *,
        tier: str | None,
        chunk_request: AllocationChunkRequest,
        resources: AvailableResources,
    ) -> PlannedTask | None:
        return self._planning.plan_retry(
            tier=tier,
            chunk_request=chunk_request,
            resources=resources,
        )

    def enqueue(
        self,
        group_id: str,
        tasks: list[PlannedTask],
        context: DispatchContext,
    ) -> list[DispatchResult]:
        return self._dispatch_queue.enqueue(group_id, tasks, context)

    def enqueue_pending(self, item: AllocationPendingItem) -> None:
        self._dispatch_queue.enqueue_pending(item)
