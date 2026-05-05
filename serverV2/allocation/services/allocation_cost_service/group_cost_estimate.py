"""GroupCostEstimate -- frozen-at-dispatch cost summary for a render group.

Static value: the SUM of per-chunk estimates the ``AllocationPlanner``
stamped onto every ``jobs`` row at allocation time.  Same number for
the lifetime of the group -- nothing live, no telemetry.

For "what is this actually costing right now", a future
``live_projection_for_group`` returning ``GroupCostProjection`` will
combine this with telemetry once the timing-data wiring is solid.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroupCostEstimate:
    """Frozen-at-dispatch cost summary, computed by SUM/MAX/COUNT over
    the ``jobs`` table's per-chunk estimate columns.

    Fields:
      * ``chunks``             total number of jobs rows for the group
      * ``total_cost_usd``     SUM(estimated_cost_usd) -- billable total
      * ``total_seconds``      SUM(estimated_seconds) -- summed render time
                               across all chunks (NOT wall time)
      * ``wall_time_seconds``  MAX(estimated_seconds) -- chunks run in
                               parallel so the longest single chunk is
                               the wall-clock estimate
    """

    chunks: int
    total_cost_usd: float
    total_seconds: float
    wall_time_seconds: float
