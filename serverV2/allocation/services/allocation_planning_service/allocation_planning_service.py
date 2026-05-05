"""AllocationPlanningService -- the cost-intelligence module.

Owns planning (via the single ``AllocationStrategy``) and cost
projection (via ``AllocationCostAggregator``).  The planner stamps
``estimated_cost_usd`` and ``estimated_seconds`` on every PlannedTask;
this service aggregates them into a group-level GroupCostEstimate --
whether the items came from a just-now planner run (pre-submit
dry-run) or from already-stored ``jobs`` rows (post-submit live group).

The ``tier`` parameter on the planning surface is accepted for API
compatibility but ignored: cost is no longer a scoring factor and there
is only one strategy.  Tier semantics moved to dispatch-queue priority
(Phase F, future).

Stateless given its constructor deps.
"""

from __future__ import annotations

from typing import Any

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.allocation_strategies.allocation_strategy import (
    AllocationStrategy,
)
from serverV2.allocation.services.allocation_planning_service.allocation_cost_aggregator import (
    AllocationCostAggregator,
)
from serverV2.allocation.services.allocation_planning_service.group_cost_estimate import (
    GroupCostEstimate,
)
from serverV2.core.models import AvailableResources, PlannedTask


class AllocationPlanningService:

    def __init__(
        self,
        *,
        strategy: AllocationStrategy,
        cost_aggregator: AllocationCostAggregator,
    ) -> None:
        self._strategy = strategy
        self._cost_aggregator = cost_aggregator

    # ------------------------------------------------------------------
    # planning surface (used by AllocationPendingTickProcessor)
    # ------------------------------------------------------------------

    def plan_initial(
        self,
        *,
        tier: str | None = None,    # accepted for compat, ignored
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        engine: str | None = None,
        heaviness: dict | None = None,
    ) -> list[PlannedTask]:
        del tier
        return self._strategy.allocate_initial(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
        )

    def plan_retry(
        self,
        *,
        tier: str | None = None,    # accepted for compat, ignored
        chunk_request: AllocationChunkRequest,
        resources: AvailableResources,
        heaviness: dict | None = None,
    ) -> PlannedTask | None:
        del tier
        return self._strategy.allocate_retry(chunk_request, resources, heaviness=heaviness)

    # ------------------------------------------------------------------
    # cost surface (used by AllocationFacade)
    # ------------------------------------------------------------------

    def cost_for_dry_run(
        self,
        *,
        tier: str | None = None,    # accepted for compat, ignored
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        engine: str | None = None,
        heaviness: dict | None = None,
    ) -> GroupCostEstimate:
        tasks = self.plan_initial(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
        )
        items = self._cost_aggregator.from_planned_tasks(tasks)
        return self._cost_aggregator.aggregate(items)

    def cost_for_committed_jobs(
        self, rows: list[dict[str, Any]],
    ) -> GroupCostEstimate:
        items = self._cost_aggregator.from_jobs_rows(rows)
        return self._cost_aggregator.aggregate(items)
