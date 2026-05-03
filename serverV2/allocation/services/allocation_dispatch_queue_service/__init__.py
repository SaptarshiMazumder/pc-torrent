"""AllocationDispatchQueueService — public service split into SRP-pure
collaborators.

Public symbol:
  * ``AllocationDispatchQueueService`` — thin entry; delegates to the
    enqueue handler and the dispatch handler.

Internal collaborators (not exported):
  * ``AllocationEnqueueHandler``      — enqueue path
  * ``AllocationDispatchHandler``     — cap-gated dispatch loop
  * ``AllocationSnapshotMutator``     — post-dispatch snapshot updates
  * ``AllocationQueueItemCodec``      — PlannedTask <-> AllocationQueueItem
  * ``AllocationEngineResolver``      — engine string from overrides JSON
"""

from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_dispatch_queue_service import (
    AllocationDispatchQueueService,
)

__all__ = ["AllocationDispatchQueueService"]
