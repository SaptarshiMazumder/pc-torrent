"""AllocationPlanningService — tier-driven strategy selection + execution.

Caller passes the user-selected ``tier`` plus the scene profile; the
service runs the matching strategy.  The tier -> strategy mapping is
allocation business, not orchestration business — the orchestrator
forwards the user's tier and walks away.

Strategies are stateless given their constructor deps; the service
owns the {name -> strategy} dict and the selector and is itself
stateless beyond that.
"""

from __future__ import annotations

from typing import Any

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_strategy_selector import (
    AllocationStrategySelector,
)
from serverV2.core.models import AvailableResources, PlannedTask


class AllocationPlanningService:

    def __init__(
        self,
        *,
        strategies: dict[str, Any],
        selector: AllocationStrategySelector,
    ) -> None:
        self._strategies = strategies
        self._selector = selector

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
        file_size_bytes = int((heaviness or {}).get("file_size_bytes", 0) or 0)
        strategy_name = self._selector.select_name(
            tier=tier,
            file_size_bytes=file_size_bytes,
            total_frames=total_frames,
        )
        strategy = self._get(strategy_name)
        return strategy.allocate_initial(
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
        strategy_name = self._selector.select_name(
            tier=tier,
            file_size_bytes=chunk_request.file_size_bytes or 0,
            total_frames=chunk_request.total_frames,
        )
        strategy = self._get(strategy_name)
        return strategy.allocate_retry(chunk_request, resources)

    def _get(self, name: str):
        try:
            return self._strategies[name]
        except KeyError:
            known = ", ".join(sorted(self._strategies.keys()))
            raise ValueError(
                f"Unknown allocation strategy: {name!r}. "
                f"Registered strategies: [{known}]"
            )
