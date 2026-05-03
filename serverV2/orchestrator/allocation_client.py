"""AllocationClient — orchestrator-side gateway to the allocation module.

The ONLY thing in the entire codebase allowed to import
``AllocationFacade``.  Inside orchestrator, only ``Lifecycle`` and
``RetryPipelineRunner`` (and per-step retry/termination steps) reach
this client.

Responsibilities:
  * Fetch the cached fleet-availability snapshot via
    ``FleetAvailabilitySnapshotCache``.
  * Adapt the snapshot to the ``AvailableResources`` shape strategies
    expect (community + serverless merged + in-flight dict).
  * Apply orchestrator-side filters (machine_ids pinning) before
    delegating to the facade.
  * Forward ``enqueue`` calls to the facade with no logic added.

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
    # planning
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
        log.info(
            "[RETRY_DEBUG] AllocationClient.plan_retry: tier=%s "
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
        result = self._facade.plan_retry(
            tier=tier,
            chunk_request=chunk_request,
            resources=resources,
        )
        log.info(
            "[RETRY_DEBUG] AllocationClient.plan_retry: facade returned %s",
            "None (no eligible target)" if result is None
            else f"PlannedTask(fleet={result.fleet}, gpu={result.gpu_type})",
        )
        return result

    # ------------------------------------------------------------------
    # enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        group_id: str,
        tasks: list[PlannedTask],
        context: DispatchContext,
    ) -> list[DispatchResult]:
        return self._facade.enqueue(group_id, tasks, context)

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
