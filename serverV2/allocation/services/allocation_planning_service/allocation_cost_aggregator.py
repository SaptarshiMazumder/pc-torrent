"""AllocationCostAggregator -- private helper of the planning service.

Pure arithmetic over a list of ``CostBearingItem``.  Has zero
knowledge of planning, heaviness, fleets, scoring, or I/O.

Surface:

  * ``aggregate(items)``           SUM/MAX/COUNT -> GroupCostEstimate
  * ``from_planned_tasks(tasks)``  PlannedTask projection helper
  * ``from_jobs_rows(rows)``       jobs-table-row projection helper

The two projection helpers live here because the aggregator's input
shape (``CostBearingItem``) is its concern, and there are exactly two
places it gets fed from.  Both pre-submit dry-run and post-submit
live-group cost paths converge on the same arithmetic.

Stateless.  Used only by ``AllocationPlanningService``.
"""

from __future__ import annotations

from typing import Any

from serverV2.allocation.services.allocation_planning_service.cost_bearing_item import (
    CostBearingItem,
)
from serverV2.allocation.services.allocation_planning_service.group_cost_estimate import (
    GroupCostEstimate,
)
from serverV2.config import FailureRateConfig
from serverV2.core.models import PlannedTask


class AllocationCostAggregator:

    def aggregate(self, items: list[CostBearingItem]) -> GroupCostEstimate:
        if not items:
            return GroupCostEstimate(
                chunks=0,
                total_cost_usd=0.0,
                total_seconds=0.0,
                wall_time_seconds=0.0,
            )
        return GroupCostEstimate(
            chunks=len(items),
            total_cost_usd=sum(i.estimated_cost_usd for i in items),
            total_seconds=sum(i.estimated_seconds for i in items),
            wall_time_seconds=max(i.estimated_seconds for i in items),
        )

    def from_planned_tasks(
        self,
        tasks: list[PlannedTask],
        failure_rate: FailureRateConfig | None = None,
    ) -> list[CostBearingItem]:
        """Project PlannedTasks to CostBearingItems.

        ``failure_rate`` (optional): per-fleet first-attempt failure
        probability.  When supplied, each task's cost and seconds are
        widened by ``(1 + p_fleet)`` -- a chunk has ``p`` chance of
        retrying once on a fresh container (full startup + render),
        roughly doubling that chunk's contribution.  Used by the
        dry-run preview so the UI doesn't advertise the happy-path-only
        number; NOT applied to ``from_jobs_rows`` (that path reads
        actuals, retries are already in the data).
        """
        items: list[CostBearingItem] = []
        for t in tasks:
            cost = float(t.estimated_cost_usd or 0.0)
            secs = float(t.estimated_seconds or 0.0)
            if failure_rate is not None:
                p = failure_rate.for_fleet(t.fleet)
                cost = cost * (1.0 + p)
                secs = secs * (1.0 + p)
            items.append(CostBearingItem(
                estimated_cost_usd=cost,
                estimated_seconds=secs,
            ))
        return items

    def from_jobs_rows(
        self, rows: list[dict[str, Any]],
    ) -> list[CostBearingItem]:
        return [
            CostBearingItem(
                estimated_cost_usd=float(r.get("estimated_cost_usd") or 0.0),
                estimated_seconds=float(r.get("estimated_seconds") or 0.0),
            )
            for r in rows
        ]
