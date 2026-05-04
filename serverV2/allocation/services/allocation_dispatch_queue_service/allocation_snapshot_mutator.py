"""AllocationSnapshotMutator -- fleet-specific snapshot mutations.

Called by the pending-queue planner immediately after it enqueues a
``PlannedTask`` to ``dispatch_queue``.  The mutation is the
"commitment" -- once the snapshot reflects the resource as taken, the
next iteration of the planner's loop (within the SAME daemon tick)
sees it gone and won't pick the same target again.

That makes the planner's loop race-free intra-tick: every promotion
mutates the shared mutable snapshot before the next row is planned.

Branches by fleet name; raises on unknown fleets so misconfiguration
fails loud rather than silently mis-counting.
"""

from __future__ import annotations

from serverV2.core.models import PlannedTask
from serverV2.fleets.fleet_availability.mutable_fleet_availability_snapshot import (
    MutableFleetAvailabilitySnapshot,
)


class AllocationSnapshotMutator:

    @staticmethod
    def mark_planned_dispatched(
        snapshot: MutableFleetAvailabilitySnapshot,
        task: PlannedTask,
    ) -> None:
        if task.fleet == "vast_serverless":
            snapshot.mark_vast_dispatched(task.gpu_type)
        elif task.fleet == "modal_serverless":
            snapshot.mark_modal_dispatched(task.gpu_type)
        elif task.fleet == "community":
            snapshot.mark_community_dispatched(task.machine_id)
        else:
            raise ValueError(
                f"AllocationSnapshotMutator: unknown fleet={task.fleet!r}"
            )
