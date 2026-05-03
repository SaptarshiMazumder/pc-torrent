"""AllocationDispatchQueueService — public service.

Two methods, both thin delegations:

  * ``enqueue``                       -> AllocationEnqueueHandler
  * ``dispatch_pending_for_fleet``    -> AllocationDispatchHandler

The class composes the read-side and write-side handlers (and their
shared helpers) and exposes them under one entry the facade + daemon
can hold a single reference to.

Cap math comes from the injected ``active_count_by_fleet`` callable
(bound at boot to ``JobRepository.count_active_by_fleet``).  No DB
access from this class itself — all I/O is in the handlers.
"""

from __future__ import annotations

from typing import Callable

from serverV2.allocation.allocation_blend_url_resolver import (
    AllocationBlendUrlResolver,
)
from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationDispatchQueueRepository,
)
from serverV2.allocation.allocation_dispatcher import AllocationDispatcher
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_dispatch_handler import (
    AllocationDispatchHandler,
)
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_engine_resolver import (
    AllocationEngineResolver,
)
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_enqueue_handler import (
    AllocationEnqueueHandler,
)
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_queue_item_codec import (
    AllocationQueueItemCodec,
)
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_snapshot_mutator import (
    AllocationSnapshotMutator,
)
from serverV2.core.models import DispatchContext, DispatchResult, PlannedTask
from serverV2.fleets.fleet_availability.mutable_fleet_availability_snapshot import (
    MutableFleetAvailabilitySnapshot,
)
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository


class AllocationDispatchQueueService:

    def __init__(
        self,
        *,
        queue_repo: AllocationDispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        active_count_by_fleet: Callable[[], dict[str, int]],
        dispatcher: AllocationDispatcher,
        blend_url_resolver: AllocationBlendUrlResolver,
        fleet_caps: dict[str, int],
        is_group_terminal: Callable[[str], bool],
    ) -> None:
        codec = AllocationQueueItemCodec()
        engine_resolver = AllocationEngineResolver()
        snapshot_mutator = AllocationSnapshotMutator()
        self._enqueue_handler = AllocationEnqueueHandler(
            queue_repo=queue_repo,
            in_progress_repo=in_progress_repo,
            codec=codec,
        )
        self._dispatch_handler = AllocationDispatchHandler(
            queue_repo=queue_repo,
            in_progress_repo=in_progress_repo,
            active_count_by_fleet=active_count_by_fleet,
            dispatcher=dispatcher,
            blend_url_resolver=blend_url_resolver,
            fleet_caps=fleet_caps,
            is_group_terminal=is_group_terminal,
            codec=codec,
            engine_resolver=engine_resolver,
            snapshot_mutator=snapshot_mutator,
        )

    def enqueue(
        self,
        group_id: str,
        tasks: list[PlannedTask],
        context: DispatchContext,
    ) -> list[DispatchResult]:
        return self._enqueue_handler.enqueue(group_id, tasks, context)

    def has_any_for_fleets(self, fleets: list[str]) -> bool:
        return self._dispatch_handler.has_any_for_fleets(fleets)

    def dispatch_pending_for_fleet(
        self,
        fleet: str,
        snapshot: MutableFleetAvailabilitySnapshot | None = None,
    ) -> int:
        return self._dispatch_handler.dispatch_pending_for_fleet(fleet, snapshot)
