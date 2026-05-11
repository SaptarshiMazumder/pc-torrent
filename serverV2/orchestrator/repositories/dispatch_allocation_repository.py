"""DispatchAllocationRepository — orchestrator-side read view of
``dispatch_queue``.

Writes on this table are owned by the allocation module
(``AllocationDispatchQueueRepository``).  This class exposes the
narrow SELECT queries the orchestration layer needs to make its own
decisions, without crossing the layer boundary into the allocation
module's repos.

Currently exposes one method, ``has_dispatch_queued_for_group``,
called by the group-status aggregator caller alongside
``PendingAllocationRepository.has_pending_for_group``.  Together they
let the aggregator treat the pending->dispatch handoff window as
active work, so an audit-sweep reconcile in that window doesn't
flip the group terminal and trigger
``TerminalGroupResourceReleaser`` to drain the in-flight row.

If a future orchestrator-side caller needs another narrow read on
``allocation_dispatch_queue``, add a method here.  Do not import
``AllocationDispatchQueueRepository`` from outside ``serverV2/allocation/``.
"""

from __future__ import annotations

from serverV2.infrastructure.db import execute_returning


class DispatchAllocationRepository:

    def has_dispatch_queued_for_group(self, group_id: str) -> bool:
        """True iff at least one ``dispatch_queue`` row belongs to
        ``group_id``.  Cheap LIMIT-1 existence check.

        See module docstring for why the aggregator needs this in
        addition to the pending-queue check.
        """
        row = execute_returning(
            "SELECT 1 AS x FROM dispatch_queue "
            "WHERE group_id = %s LIMIT 1",
            (group_id,),
        )
        return row is not None
