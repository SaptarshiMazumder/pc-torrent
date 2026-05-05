"""AllocationFacade -- the only public symbol of the allocation module.

Public entry points:

  Writes (parking lot):

    * ``submit_initial`` -- park a whole render group on
      ``pending_allocation_queue``.  The dispatch daemon plans + enqueues
      on its next tick.
    * ``park_retry`` -- park a single retry chunk on
      ``pending_allocation_queue``.  Auto-retry (callback threads) and
      manual-retry (HTTP threads) both go through here.
    * ``drain_for_group`` -- cancel-pipeline cleanup; wipes the group's
      rows from BOTH queues.

  Reads (cost intelligence):

    * ``cost_estimate_for_group`` -- post-submit live group.  Sums the
      per-chunk estimates the planner stamped onto every ``jobs`` row
      at planning time.  Same number for the lifetime of the group.
    * ``cost_estimate_for_dry_run`` -- pre-submit preview.  Asks the
      planning service to plan against the cached fleet-availability
      snapshot, projects the resulting ``PlannedTask`` list, and sums
      it.  No DB writes; the cache is read-only on this path
      (``get_or_build`` only -- no ``persist``).

Architectural invariants:

  * Writes NEVER touch ``dispatch_queue`` directly.  All writes park to
    ``pending_allocation_queue``; the dispatch daemon is the sole
    writer of ``dispatch_queue`` (via its tick-local mutable snapshot).
    This eliminates every cross-thread race between planning and
    dispatching.
  * Cost paths NEVER mutate state.  No persists, no DB writes -- the
    facade is read-only on the cost surface.

Constructed once at boot; stateless beyond the injected references.
"""

from __future__ import annotations

from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationDispatchQueueRepository,
)
from serverV2.allocation.allocation_pending_queue_repository import (
    AllocationPendingItem,
    AllocationPendingQueueRepository,
    TYPE_INITIAL_GROUP,
    TYPE_RETRY_CHUNK,
)
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.services.allocation_planning_service import (
    AllocationPlanningService,
    GroupCostEstimate,
)
from serverV2.core.models import (
    AvailableResources,
    DispatchContext,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot import (
    FleetAvailabilitySnapshot,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot_cache import (
    FleetAvailabilitySnapshotCache,
)
from serverV2.repositories.job_repository import JobRepository


class AllocationFacade:

    def __init__(
        self,
        *,
        planning: AllocationPlanningService,
        pending_repo: AllocationPendingQueueRepository,
        dispatch_repo: AllocationDispatchQueueRepository,
        job_repository: JobRepository,
        snapshot_cache: FleetAvailabilitySnapshotCache,
    ) -> None:
        self._planning = planning
        self._pending_repo = pending_repo
        self._dispatch_repo = dispatch_repo
        self._job_repo = job_repository
        self._snapshot_cache = snapshot_cache

    # ------------------------------------------------------------------
    # writes -- parking lot
    # ------------------------------------------------------------------

    def submit_initial(
        self,
        *,
        group_id: str,
        tier: str | None,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        engine: str | None,
        heaviness: dict | None,
        machine_ids: list[str] | None,
        dispatch_context: DispatchContext,
    ) -> None:
        """Park a whole render group.  Daemon plans + enqueues on its
        next tick.  ``heaviness`` accepted for caller convenience but
        no longer persisted on the row -- the daemon reads canonical
        heaviness from ``render_groups.resolved_scene_json`` at tick
        time, so any heaviness stamped here would just go stale."""
        del heaviness  # no longer persisted on the pending row
        self._pending_repo.enqueue(AllocationPendingItem(
            type=TYPE_INITIAL_GROUP,
            group_id=group_id,
            chunk_index=None,
            attempt=None,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            engine=engine,
            tier=tier,
            machine_ids=tuple(machine_ids or ()),
            render_overrides_json=dispatch_context.render_overrides_json,
            max_retries=dispatch_context.max_retries,
            priority=dispatch_context.priority,
            input_filename=dispatch_context.input_filename,
        ))

    def park_retry(
        self,
        *,
        chunk_request: AllocationChunkRequest,
        tier: str | None,
        dispatch_context: DispatchContext,
    ) -> None:
        """Park a single retry attempt on ``pending_allocation_queue``.
        No synchronous planning -- the daemon plans against its
        tick-local mutable snapshot."""
        self._pending_repo.enqueue(AllocationPendingItem(
            type=TYPE_RETRY_CHUNK,
            group_id=chunk_request.group_id,
            chunk_index=chunk_request.chunk_index,
            attempt=chunk_request.attempt,
            frame_start=chunk_request.frame_start,
            frame_end=chunk_request.frame_end,
            frame_step=chunk_request.frame_step,
            total_frames=chunk_request.total_frames,
            engine=chunk_request.engine,
            tier=tier,
            excluded_machine_ids=chunk_request.excluded_machine_ids,
            excluded_serverless_capabilities=chunk_request.excluded_serverless_capabilities,
            render_overrides_json=dispatch_context.render_overrides_json,
            max_retries=dispatch_context.max_retries,
            priority=dispatch_context.priority,
            input_filename=dispatch_context.input_filename,
        ))

    def drain_for_group(self, group_id: str) -> int:
        """Remove every queued row (dispatch + pending) for ``group_id``.
        Cancel pipeline calls this through ``AllocationClient`` so it
        never imports the queue repos directly.  Returns total rows
        deleted across both tables."""
        dispatched = self._dispatch_repo.drain(group_id)
        pending = self._pending_repo.delete_for_group(group_id)
        return dispatched + pending

    # ------------------------------------------------------------------
    # reads -- cost intelligence
    # ------------------------------------------------------------------

    def cost_estimate_for_group(self, group_id: str) -> GroupCostEstimate:
        """Sum the per-chunk estimates the planner stamped at planning
        time onto every ``jobs`` row for this group.  Returns an
        all-zero estimate if no jobs exist yet (e.g. group still parked
        in ``pending_allocation_queue`` and the daemon hasn't promoted
        it yet); callers can check ``chunks == 0`` for that case."""
        rows = self._job_repo.get_raw_by_group(group_id)
        return self._planning.cost_for_committed_jobs(rows)

    def cost_estimate_for_dry_run(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        engine: str | None = None,
        heaviness: dict | None = None,
    ) -> GroupCostEstimate:
        """Pre-submit cost preview for a hypothetical group.  Reads the
        cached fleet-availability snapshot (no persist), runs the
        planner against the user's heaviness, and aggregates the
        resulting per-task estimates.  No DB reads, no DB writes."""
        snapshot = self._snapshot_cache.get_or_build()
        resources = _adapt_frozen_snapshot_to_resources(snapshot)
        return self._planning.cost_for_dry_run(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
        )


def _adapt_frozen_snapshot_to_resources(
    snapshot: FleetAvailabilitySnapshot,
) -> AvailableResources:
    """Adapt a frozen fleet-availability snapshot into the resource
    shape strategies consume.  No machine_ids pinning -- the dry-run
    cost path is unpinned by construction (preview is for choosing,
    not for reserving)."""
    return AvailableResources(
        community_machines=list(snapshot.community_available),
        serverless_capabilities=(
            list(snapshot.vast_available) + list(snapshot.modal_available)
        ),
        serverless_in_flight=dict(snapshot.serverless_in_flight),
    )
