"""AllocationCostService -- read-side aggregation of per-chunk estimates.

Reads ONLY.  No writes.  The estimate columns
(``estimated_seconds`` / ``estimated_cost_usd`` / etc.) are stamped
onto each ``jobs`` row by ``AllocationPlanner`` at planning time;
this service is the canonical place to aggregate them across a render
group.

Public surface today:

  * ``static_estimate_for_group(group_id) -> GroupCostEstimate``
    Frozen-at-dispatch SUM/MAX/COUNT over the group's chunks.  Same
    number for the lifetime of the group.

A future ``live_projection_for_group`` returning ``GroupCostProjection``
will combine these estimates with telemetry (started_at, completed_at,
observed frame uploads) to project current spend + remaining cost.
That is intentionally NOT implemented yet -- the timing-data wiring
needed to do it correctly is being fixed separately, and a half-good
projection is worse than none.
"""

from __future__ import annotations

import logging

from serverV2.allocation.services.allocation_cost_service.group_cost_estimate import (
    GroupCostEstimate,
)
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)


class AllocationCostService:

    def __init__(self, *, job_repo: JobRepository) -> None:
        self._job_repo = job_repo

    def static_estimate_for_group(self, group_id: str) -> GroupCostEstimate:
        """Sum the per-chunk estimates the planner stamped at dispatch.

        Returns ``GroupCostEstimate`` with all-zero fields if the group
        has no jobs yet (e.g. parked on ``pending_allocation_queue``
        and the daemon hasn't promoted it yet).  Callers can check
        ``chunks == 0`` to detect that case.
        """
        agg = self._job_repo.get_cost_aggregate_for_group(group_id)
        return GroupCostEstimate(
            chunks=agg["chunks"],
            total_cost_usd=agg["total_cost_usd"],
            total_seconds=agg["total_seconds"],
            wall_time_seconds=agg["wall_time_seconds"],
        )
