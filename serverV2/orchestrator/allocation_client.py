"""AllocationClient -- orchestrator-side gateway to the allocation module.

The ONLY thing in the entire codebase allowed to import
``AllocationFacade``.  Inside orchestrator, callers reach this client
to (a) dry-run a plan for the cost preview / pre-render estimate,
(b) submit a render group for dispatch (initial), (c) park a retry
attempt, or (d) drain queues on cancel.

Responsibilities:
  * Fetch the cached fleet-availability snapshot via
    ``FleetAvailabilitySnapshotCache``.
  * Adapt the snapshot to the ``AvailableResources`` shape strategies
    expect (community + serverless merged + in-flight dict).
  * Apply orchestrator-side filters (machine_ids pinning) before
    delegating to the facade.

Stateless beyond the injected facade + cache; safe to share across
requests.
"""

from __future__ import annotations

import logging

from serverV2.allocation import AllocationFacade
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.core.models import (
    AvailableResources,
    DispatchContext,
    PlannedTask,
    SubmitInitialResult,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot import (
    FleetAvailabilitySnapshot,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot_cache import (
    FleetAvailabilitySnapshotCache,
)

log = logging.getLogger(__name__)


class AllocationClient:

    def __init__(
        self,
        *,
        facade: AllocationFacade,
        snapshot_cache: FleetAvailabilitySnapshotCache,
    ) -> None:
        self._facade = facade
        self._snapshot_cache = snapshot_cache

    # ------------------------------------------------------------------
    # planning (pure compute — used by the pre-render cost preview)
    # ------------------------------------------------------------------

    def plan_initial(
        self,
        *,
        tier: str | None,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        engine: str | None = None,
        heaviness: dict | None = None,
        tier_budget_usd: float | None = None,
        machine_ids: list[str] | None = None,
    ) -> list[PlannedTask]:
        snapshot = self._snapshot_cache.get_or_build()
        resources = self._adapt_resources(snapshot, machine_ids=machine_ids)
        return self._facade.plan_initial(
            tier=tier,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
            tier_budget_usd=tier_budget_usd,
        )

    # ------------------------------------------------------------------
    # submit (plan + (enqueue|park) atomically)
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
        tier_budget_usd: float | None,
        machine_ids: list[str] | None,
        dispatch_context: DispatchContext,
    ) -> SubmitInitialResult:
        snapshot = self._snapshot_cache.get_or_build()
        resources = self._adapt_resources(snapshot, machine_ids=machine_ids)
        return self._facade.submit_initial(
            group_id=group_id,
            tier=tier,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
            tier_budget_usd=tier_budget_usd,
            machine_ids=machine_ids,
            dispatch_context=dispatch_context,
        )

    def park_retry(
        self,
        *,
        chunk_request: AllocationChunkRequest,
        tier: str | None,
        dispatch_context: DispatchContext,
    ) -> None:
        """Park a retry onto ``pending_allocation_queue``.  No snapshot
        read here -- planning happens later inside the daemon's tick,
        against the tick-local mutable snapshot.  Used by both auto-
        retry (failure callback threads) and manual-retry (HTTP
        request thread); neither needs to read the cache.
        """
        self._facade.park_retry(
            chunk_request=chunk_request,
            tier=tier,
            dispatch_context=dispatch_context,
        )

    # ------------------------------------------------------------------
    # group-scoped delete (cancel path)
    # ------------------------------------------------------------------

    def drain_for_group(self, group_id: str) -> int:
        """Cancel pipeline calls this to wipe a cancelled group's stale
        rows from both queues.  Routes through the facade — the
        RenderCanceler does not import either queue repo directly."""
        return self._facade.drain_for_group(group_id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _adapt_resources(
        snapshot: FleetAvailabilitySnapshot,
        *,
        machine_ids: list[str] | None,
    ) -> AvailableResources:
        community = list(snapshot.community_available)
        if machine_ids:
            wanted = set(machine_ids)
            return AvailableResources(
                community_machines=[m for m in community if m.id in wanted],
                serverless_capabilities=[],
                serverless_in_flight=snapshot.serverless_in_flight,
            )
        return AvailableResources(
            community_machines=community,
            serverless_capabilities=(
                list(snapshot.vast_available) + list(snapshot.modal_available)
            ),
            serverless_in_flight=snapshot.serverless_in_flight,
        )
