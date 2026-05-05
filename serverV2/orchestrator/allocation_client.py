"""AllocationClient -- orchestrator-side gateway to the allocation module.

The ONLY thing in the entire codebase allowed to import
``AllocationFacade``.  Inside orchestrator, callers reach this client
to (a) submit a render group for dispatch, (b) park a retry attempt,
(c) drain queues on cancel, or (d) ask for cost estimates -- pre-submit
preview or post-submit live group.

Stateless beyond the injected facade reference; safe to share across
requests.  The facade owns its own snapshot reads and adapter logic
for the dry-run cost path; this client is a pure pass-through.
"""

from __future__ import annotations

import logging

from serverV2.allocation import AllocationFacade
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.services.allocation_planning_service import (
    GroupCostEstimate,
)
from serverV2.core.models import DispatchContext

log = logging.getLogger(__name__)


class AllocationClient:

    def __init__(self, *, facade: AllocationFacade) -> None:
        self._facade = facade

    # ------------------------------------------------------------------
    # writes -- park to pending_allocation_queue
    # ------------------------------------------------------------------

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
        self._facade.submit_initial(
            group_id=group_id,
            tier=tier,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            engine=engine,
            heaviness=heaviness,
            machine_ids=machine_ids,
            dispatch_context=dispatch_context,
        )

    def park_retry(
        self,
        *,
        chunk_request: AllocationChunkRequest,
        tier: str | None,
        dispatch_context: DispatchContext,
    ) -> None:
        self._facade.park_retry(
            chunk_request=chunk_request,
            tier=tier,
            dispatch_context=dispatch_context,
        )

    def drain_for_group(self, group_id: str) -> int:
        return self._facade.drain_for_group(group_id)

    # ------------------------------------------------------------------
    # reads -- cost intelligence
    # ------------------------------------------------------------------

    def cost_estimate_for_group(self, group_id: str) -> GroupCostEstimate:
        return self._facade.cost_estimate_for_group(group_id)

    def cost_estimate_for_dry_run(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        engine: str | None = None,
        heaviness: dict | None = None,
    ) -> GroupCostEstimate:
        return self._facade.cost_estimate_for_dry_run(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            engine=engine,
            heaviness=heaviness,
        )
