"""AllocationPendingTickProcessor -- one phase of the daemon's tick.

Called by ``AllocationDispatchQueueDaemon._tick`` once per tick after
the dispatch phase.  Walks the ``pending_allocation_queue``, plans
each row through the strategies, and writes the resulting tasks to
``dispatch_queue`` -- mutating the in-tick mutable snapshot per pick
so subsequent rows see committed resources.

Per-row work:

  1. Look up row type (TYPE_INITIAL_GROUP or TYPE_RETRY_CHUNK).
  2. Read the FULL heaviness sub-dict from
     ``render_groups.resolved_scene_json`` (one row read per pending
     item).  This is the canonical scene context the planner reads
     for cost / time / VRAM math; both initial and retry rows go
     through the same fetch.
  3. Adapt the mutable snapshot to ``AvailableResources`` for the
     strategy's consumption.
  4. Call ``AllocationPlanningService.plan_initial`` /
     ``plan_retry``; receive ``list[PlannedTask]``.
  5. For each task:
        - skip if the in-progress ledger already owns the chunk
        - generate a fresh job_id
        - encode PlannedTask -> AllocationQueueItem via codec
        - write the row to ``dispatch_queue``
  6. ``mark_planned_dispatched`` on the snapshot for each task --
     the COMMITMENT.  Next pending row in the same loop sees the
     resource as taken and won't pick it again.
  7. Delete the pending row.

Race-free intra-tick by construction: the mutation in step 6
happens BEFORE the next row's planning in step 4.

This is the FIRST and ONLY place pending rows get planned.  All
allocation entry points (initial submit, auto-retry, manual-retry)
park to ``pending_allocation_queue`` first; the daemon picks them up
here.  Nothing was "previously planned" -- so no "re" anywhere.
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from uuid import uuid4

from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationDispatchQueueRepository,
)
from serverV2.allocation.allocation_pending_queue_repository import (
    AllocationPendingItem,
    AllocationPendingQueueRepository,
    TYPE_INITIAL_GROUP,
    TYPE_RETRY_CHUNK,
)
from serverV2.allocation.allocation_queue_item_codec import (
    AllocationQueueItemCodec,
)
from serverV2.allocation.allocation_snapshot_mutator import (
    AllocationSnapshotMutator,
)
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.services.allocation_planning_service import (
    AllocationPlanningService,
)
from serverV2.core.models import (
    AvailableResources,
    DispatchContext,
    PlannedTask,
)
from serverV2.fleets.fleet_availability.mutable_fleet_availability_snapshot import (
    MutableFleetAvailabilitySnapshot,
)
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


class AllocationPendingTickProcessor:

    def __init__(
        self,
        *,
        pending_repo: AllocationPendingQueueRepository,
        dispatch_repo: AllocationDispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        render_group_repository: RenderGroupRepository,
        planning_service: AllocationPlanningService,
        codec: AllocationQueueItemCodec,
        snapshot_mutator: AllocationSnapshotMutator,
        affinity_for_group: Callable[[str], Any] | None = None,
    ) -> None:
        self._pending_repo = pending_repo
        self._dispatch_repo = dispatch_repo
        self._in_progress = in_progress_repo
        self._rg_repo = render_group_repository
        self._planning = planning_service
        self._codec = codec
        self._snapshot_mutator = snapshot_mutator
        # Injected orchestrator AffinityFacade.affinity_for_group (a callable
        # so this layer never imports orchestrator).  Resolves a group's
        # preferred placements fresh at plan time.  None -> no affinity
        # (tests / not wired); retries fall back to the normal selection.
        self._affinity_for_group = affinity_for_group

    def has_any(self) -> bool:
        """Idle-tick guard: returns True iff at least one row is parked
        in ``pending_allocation_queue``."""
        return self._pending_repo.has_any()

    def process(self, snapshot: MutableFleetAvailabilitySnapshot) -> int:
        """One pass over the pending queue.  Returns the number of rows
        promoted to ``dispatch_queue``."""
        moved = 0
        rows = self._pending_repo.list_all()
        for row in rows:
            try:
                if self._try_promote(row, snapshot):
                    moved += 1
                else:
                    self._pending_repo.update_last_attempted_at(row.id)  # type: ignore[arg-type]
            except Exception as exc:
                log.error(
                    "AllocationPendingTickProcessor: row %s (type=%s, group=%s) raised: %r",
                    row.id, row.type, row.group_id, exc,
                )
        if moved:
            log.info(
                "AllocationPendingTickProcessor: promoted %d row(s) from pending to dispatch_queue",
                moved,
            )
        return moved

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _try_promote(
        self,
        row: AllocationPendingItem,
        snapshot: MutableFleetAvailabilitySnapshot,
    ) -> bool:
        if row.type == TYPE_RETRY_CHUNK:
            tasks = self._plan_for_retry(row, snapshot)
        elif row.type == TYPE_INITIAL_GROUP:
            tasks = self._plan_for_initial(row, snapshot)
        else:
            log.warning(
                "AllocationPendingTickProcessor: unknown row type=%r (id=%s) -- skipping",
                row.type, row.id,
            )
            return False

        if not tasks:
            return False

        ctx = DispatchContext(
            group_id=row.group_id,
            input_filename=row.input_filename,
            render_overrides_json=row.render_overrides_json,
            blend_url="",
            max_retries=row.max_retries,
            priority=row.priority,
            engine=row.engine,
        )

        # Inline enqueue (was AllocationEnqueueHandler -- only one caller
        # post-refactor, no need for a separate class).  For each task:
        # check the in-progress ledger first; skip duplicates; otherwise
        # generate a job_id, encode, and write the dispatch_queue row.
        for task in tasks:
            chunk_index = task.chunk_index or 0
            owner = self._in_progress.current_job_for(row.group_id, chunk_index)
            if owner is not None:
                log.info(
                    "Group %s chunk %s already in progress (job %s) -- skipping enqueue",
                    row.group_id, chunk_index, owner,
                )
                continue
            job_id = str(uuid4())
            self._dispatch_repo.enqueue(
                row.group_id,
                self._codec.encode(task, ctx, job_id=job_id),
            )

        # Commit each planned target into the in-tick mutable snapshot
        # immediately.  This is what prevents the dogpile: the next
        # row in the planner's loop sees the resource as taken and
        # the strategy picks something else.
        for task in tasks:
            self._snapshot_mutator.mark_planned_dispatched(snapshot, task)
        self._pending_repo.delete(row.id)  # type: ignore[arg-type]
        return True

    def _plan_for_retry(
        self,
        row: AllocationPendingItem,
        snapshot: MutableFleetAvailabilitySnapshot,
    ) -> list[PlannedTask]:
        # Affinity is resolved FRESH here (not persisted on the parked row):
        # the daemon is the one and only place a retry is planned, so this is
        # the latest "which combos have started/rendered this group" signal.
        # Empty when nothing has started yet, or when no resolver is wired.
        preferred_caps: tuple = ()
        preferred_ids: tuple = ()
        if self._affinity_for_group is not None:
            aff = self._affinity_for_group(row.group_id)
            preferred_caps = tuple(aff.preferred_serverless_capabilities)
            preferred_ids = tuple(aff.preferred_machine_ids)
        chunk_request = AllocationChunkRequest(
            group_id=row.group_id,
            chunk_index=row.chunk_index or 0,
            frame_start=row.frame_start,
            frame_end=row.frame_end,
            frame_step=row.frame_step,
            total_frames=row.total_frames,
            attempt=row.attempt or 0,
            excluded_machine_ids=row.excluded_machine_ids,
            excluded_serverless_capabilities=row.excluded_serverless_capabilities,
            preferred_serverless_capabilities=preferred_caps,
            preferred_machine_ids=preferred_ids,
            engine=row.engine,
        )
        resources = _adapt_resources(snapshot, machine_ids=None)
        heaviness = self._load_heaviness(row.group_id)
        task = self._planning.plan_retry(
            tier=row.tier,
            chunk_request=chunk_request,
            resources=resources,
            heaviness=heaviness,
        )
        return [task] if task is not None else []

    def _plan_for_initial(
        self,
        row: AllocationPendingItem,
        snapshot: MutableFleetAvailabilitySnapshot,
    ) -> list[PlannedTask]:
        heaviness = self._load_heaviness(row.group_id)
        resources = _adapt_resources(
            snapshot,
            machine_ids=list(row.machine_ids) if row.machine_ids else None,
        )
        return self._planning.plan_initial(
            tier=row.tier,
            frame_start=row.frame_start,
            frame_end=row.frame_end,
            frame_step=row.frame_step,
            total_frames=row.total_frames,
            resources=resources,
            engine=row.engine,
            heaviness=heaviness,
        )

    def _load_heaviness(self, group_id: str) -> dict:
        """Fetch the canonical heaviness dict the planner reads.  Source
        of truth: ``render_groups.resolved_scene_json``, persisted by
        ``RenderGroupService`` at confirm-upload time via
        ``SceneResolver``.  Both initial and retry pending rows share
        this fetch so heaviness is identical to what the user's tier
        choice was costed against at submit time."""
        return self._rg_repo.get_resolved_heaviness(group_id)


def _adapt_resources(
    snapshot: MutableFleetAvailabilitySnapshot,
    *,
    machine_ids: list[str] | None,
) -> AvailableResources:
    """Construct ``AvailableResources`` from the in-tick mutable
    snapshot for the strategies to consume.  Mirrors the shape of
    ``AllocationClient._adapt_resources`` (cost-preview path) but
    reads from the daemon's mutable view rather than the cache.
    """
    community = list(snapshot.community_available)
    if machine_ids:
        wanted = set(machine_ids)
        return AvailableResources(
            community_machines=[m for m in community if m.id in wanted],
            serverless_capabilities=[],
            serverless_in_flight=dict(snapshot.serverless_in_flight),
        )
    return AvailableResources(
        community_machines=community,
        serverless_capabilities=(
            list(snapshot.vast_available) + list(snapshot.modal_available)
        ),
        serverless_in_flight=dict(snapshot.serverless_in_flight),
    )
