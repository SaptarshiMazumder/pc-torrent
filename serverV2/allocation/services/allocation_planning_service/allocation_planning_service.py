"""AllocationPlanningService -- the cost-intelligence module.

Owns BOTH planning and cost projection: the planner is the only thing
that calculates per-chunk cost (it stamps ``estimated_cost_usd`` and
``estimated_seconds`` on every ``PlannedTask``), and this service is
the only place those numbers get aggregated into a group-level
``GroupCostEstimate`` -- whether the items came from a just-now
planner run (pre-submit dry-run) or from already-stored ``jobs`` rows
(post-submit live group).

Public surface:

  Planning (used by the daemon's pending tick processor):

    * ``plan_initial`` -- whole-group planning.  list[PlannedTask].
    * ``plan_retry``   -- single-chunk re-planning.  PlannedTask | None.

  Cost (used by ``AllocationFacade``):

    * ``cost_for_dry_run`` -- pre-submit preview.  Plans, projects, sums.
    * ``cost_for_committed_jobs`` -- post-submit live group.  Sums over
      jobs rows whose estimates were stamped at planning time.

Tier -> strategy mapping is handled by ``AllocationStrategySelector``;
projection + summation by ``AllocationCostAggregator``.  This class
just routes between them.

Stateless given its constructor deps.
"""

from __future__ import annotations

from typing import Any

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_strategy_selector import (
    AllocationStrategySelector,
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
        strategies: dict[str, Any],
        selector: AllocationStrategySelector,
        cost_aggregator: AllocationCostAggregator,
    ) -> None:
        self._strategies = strategies
        self._selector = selector
        self._cost_aggregator = cost_aggregator

    # ------------------------------------------------------------------
    # planning surface (used by AllocationPendingTickProcessor)
    # ------------------------------------------------------------------

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
    ) -> list[PlannedTask]:
        strategy = self._get(self._selector.select_name(tier=tier))
        return strategy.allocate_initial(
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
        tier: str | None,
        chunk_request: AllocationChunkRequest,
        resources: AvailableResources,
        heaviness: dict | None = None,
    ) -> PlannedTask | None:
        strategy = self._get(self._selector.select_name(tier=tier))
        return strategy.allocate_retry(chunk_request, resources, heaviness=heaviness)

    # ------------------------------------------------------------------
    # cost surface (used by AllocationFacade)
    # ------------------------------------------------------------------

    def cost_for_dry_run(
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
    ) -> GroupCostEstimate:
        tasks = self.plan_initial(
            tier=tier,
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

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get(self, name: str):
        try:
            return self._strategies[name]
        except KeyError:
            known = ", ".join(sorted(self._strategies.keys()))
            raise ValueError(
                f"Unknown allocation strategy: {name!r}. "
                f"Registered strategies: [{known}]"
            )
