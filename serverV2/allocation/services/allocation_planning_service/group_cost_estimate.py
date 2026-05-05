"""GroupCostEstimate -- frozen-at-planning cost summary for a render group.

The SUM/MAX/COUNT of per-chunk estimates that ``AllocationPlanner``
stamped onto every ``PlannedTask`` (and, after dispatch, onto every
``jobs`` row).  Same shape whether the source is a just-now planner
run or already-stored jobs rows.

Fields:
  * ``chunks``             total number of chunks in the group
  * ``total_cost_usd``     SUM(estimated_cost_usd) -- billable total
  * ``total_seconds``      SUM(estimated_seconds) -- summed render time
                           across all chunks (NOT wall time)
  * ``wall_time_seconds``  MAX(estimated_seconds) -- chunks run in
                           parallel so the longest single chunk is
                           the wall-clock estimate
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroupCostEstimate:
    chunks: int
    total_cost_usd: float
    total_seconds: float
    wall_time_seconds: float
