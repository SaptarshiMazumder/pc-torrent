"""CostBearingItem -- value object the cost aggregator sums over.

Both the planner's ``PlannedTask`` (pre-submit dry-run) and the
``jobs`` table row (post-submit live group) carry the same two
stamped fields: ``estimated_cost_usd`` and ``estimated_seconds``.
``AllocationCostAggregator`` projects either source into a list of
``CostBearingItem`` instances and then sums/maxes/counts without
caring which side it came from.

Frozen, no methods.  Pure value.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostBearingItem:
    estimated_cost_usd: float
    estimated_seconds: float
