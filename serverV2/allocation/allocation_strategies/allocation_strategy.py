"""AllocationStrategy -- the only strategy.

Replaces the prior Economy / Standard / Premium tier shells.  Cost is
no longer a scoring factor; tier semantics moved to dispatch-queue
priority (Phase F, future).  This class is now a thin wrapper around
``AllocationPlanner`` -- exists for API compatibility with the
planning service surface and to keep dependency injection one-step
explicit.
"""

from __future__ import annotations

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.allocation_strategies.allocation_planner import (
    AllocationPlanner,
)
from serverV2.allocation.allocation_strategies.allocation_weights import DEFAULT
from serverV2.core.models import AvailableResources, PlannedTask


class AllocationStrategy:

    WEIGHTS = DEFAULT

    def __init__(self, planner: AllocationPlanner) -> None:
        self._planner = planner

    def allocate_initial(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        engine: str | None = None,
        heaviness: dict | None = None,
    ) -> list[PlannedTask]:
        return self._planner.plan_initial(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            weights=self.WEIGHTS,
            engine=engine,
            heaviness=heaviness,
        )

    def allocate_retry(
        self,
        chunk_request: AllocationChunkRequest,
        resources: AvailableResources,
        *,
        heaviness: dict | None = None,
    ) -> PlannedTask | None:
        return self._planner.plan_retry(
            chunk_request=chunk_request,
            resources=resources,
            weights=self.WEIGHTS,
            heaviness=heaviness,
        )
