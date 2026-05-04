"""AllocationClient — orchestrator-side gateway to the allocation module.

The ONLY thing in the entire codebase allowed to import
``AllocationFacade``.  Inside orchestrator, callers reach this client
to (a) dry-run a plan for the cost preview / pre-render estimate,
(b) submit a render group for dispatch (initial), (c) submit a single
retry attempt, or (d) drain queues on cancel.

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
    DispatchResult,
    PlannedTask,
    SubmitInitialResult,
    SubmitRetryResult,
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

    def plan_retry(
        self,
        *,
        tier: str | None,
        chunk_request: AllocationChunkRequest,
    ) -> PlannedTask | None:
        snapshot = self._snapshot_cache.get_or_build()
        resources = self._adapt_resources(snapshot, machine_ids=None)
        return self._facade.plan_retry(
            tier=tier,
            chunk_request=chunk_request,
            resources=resources,
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

    def enqueue(
        self,
        group_id: str,
        tasks: list[PlannedTask],
        context: DispatchContext,
    ) -> list[DispatchResult]:
        """Enqueue an already-planned task list (no allocation step).
        Used by the manual-retry pipeline only; initial submit + auto
        retry use ``submit_*`` instead."""
        return self._facade.enqueue(group_id, tasks, context)

    def submit_retry(
        self,
        *,
        chunk_request: AllocationChunkRequest,
        tier: str | None,
        dispatch_context: DispatchContext,
    ) -> SubmitRetryResult:
        snapshot = self._snapshot_cache.get_or_build()
        resources = self._adapt_resources(snapshot, machine_ids=None)
        log.info(
            "[RETRY_DEBUG] AllocationClient.submit_retry: tier=%s "
            "snapshot(vast=%d, modal=%d, community=%d, in_flight=%s) "
            "adapted_resources(community=%d, serverless_caps=%d)",
            tier,
            len(snapshot.vast_available),
            len(snapshot.modal_available),
            len(snapshot.community_available),
            dict(snapshot.serverless_in_flight),
            len(resources.community_machines),
            len(resources.serverless_capabilities),
        )
        return self._facade.submit_retry(
            chunk_request=chunk_request,
            tier=tier,
            resources=resources,
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
