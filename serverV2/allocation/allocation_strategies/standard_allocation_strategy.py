"""StandardAllocationStrategy -- balanced tier shell.

Thin wrapper around ``AllocationPlanner`` with the STANDARD weights.
50/50 speed-vs-cost weighting.  Replaces the old DefaultAllocationStrategy
and FastRenderAllocationStrategy -- both are gone, the unified planner
covers their roles.
"""

from __future__ import annotations

from serverV2.allocation.allocation_strategies.allocation_planner import (
    AllocationPlanner,
)
from serverV2.allocation.allocation_strategies.allocation_weights import STANDARD
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.core.models import AvailableResources, PlannedTask


class StandardAllocationStrategy:

    WEIGHTS = STANDARD

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
        tier_budget_usd: float | None = None,
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
    ) -> PlannedTask | None:
        return self._planner.plan_retry(
            chunk_request=chunk_request,
            resources=resources,
            weights=self.WEIGHTS,
        )
