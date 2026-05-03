"""AllocationSnapshotMutator — fleet-specific post-dispatch updates to
the mutable fleet-availability snapshot.

One responsibility: after a queue item is successfully dispatched,
decrement the right field on the mutable snapshot so the daemon's
end-of-tick cache write reflects post-dispatch state.

Branches by fleet name; raises on unknown fleets so misconfiguration
fails loud rather than silently mis-counting.
"""

from __future__ import annotations

from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationQueueItem,
)
from serverV2.fleets.fleet_availability.mutable_fleet_availability_snapshot import (
    MutableFleetAvailabilitySnapshot,
)


class AllocationSnapshotMutator:

    @staticmethod
    def mark_dispatched(
        snapshot: MutableFleetAvailabilitySnapshot,
        item: AllocationQueueItem,
    ) -> None:
        if item.fleet == "vast_serverless":
            snapshot.mark_vast_dispatched(item.gpu_type)
        elif item.fleet == "modal_serverless":
            snapshot.mark_modal_dispatched(item.gpu_type)
        elif item.fleet == "community":
            snapshot.mark_community_dispatched(item.machine_id)
        else:
            raise ValueError(
                f"AllocationSnapshotMutator: unknown fleet={item.fleet!r}"
            )
