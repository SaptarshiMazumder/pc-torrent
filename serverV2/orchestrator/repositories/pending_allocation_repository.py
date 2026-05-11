"""PendingAllocationRepository — orchestrator-side read view of
``pending_allocation_queue``.

Writes on this table are owned by the allocation module
(``AllocationPendingQueueRepository`` and the dispatch-queue service).
This class exposes the narrow set of SELECT queries the orchestration
layer needs in order to make its own decisions — currently just a
"is this group still parked anywhere?" check used by the group-status
aggregator.

If a future orchestrator-side caller needs another read (e.g. count
parked rows for a status DTO), add a method here.  Do not import
``AllocationPendingQueueRepository`` from outside ``serverV2/allocation/``.
"""

from __future__ import annotations

from serverV2.infrastructure.db import execute_returning


class PendingAllocationRepository:

    def has_pending_for_group(self, group_id: str) -> bool:
        """True iff at least one ``pending_allocation_queue`` row
        belongs to ``group_id``.  Cheap LIMIT-1 existence check.

        The aggregator combines this with the parallel dispatch-queue
        check (``DispatchAllocationRepository.has_dispatch_queued_for_group``)
        so a group with in-flight allocation work in EITHER queue stays
        classified as active across the pending->dispatch handoff
        window -- otherwise an audit-sweep reconcile firing in that
        window would flip the group terminal and drain the in-flight
        dispatch row.
        """
        row = execute_returning(
            "SELECT 1 AS x FROM pending_allocation_queue "
            "WHERE group_id = %s LIMIT 1",
            (group_id,),
        )
        return row is not None
