"""DefaultAllocationStrategy — the baseline allocation behaviour.

Initial: one chunk per eligible target (community machine OR serverless
capability), sized proportionally by ``compute_power_score``.
Retry:   highest ``compute_power_score`` among the eligible pool, with
anti-affinity exclusions applied.

Per-fleet fan-out (multiple containers from one fleet for one render),
heaviness-aware sizing, VRAM-fit filtering, and tier rules are all
intentionally absent here — those live in
``FastRenderAllocationStrategy`` which is added in Phase 3.

Renamed from ``DefaultFrameAllocator`` as part of the Phase 1 split
into capability-based allocation (see plans/allocator_redesign_phases.md).
"""

from __future__ import annotations

from serverV2.allocation.frame_distributor import distribute_frames
from serverV2.allocation.power_scorer import compute_power_score
from serverV2.core.models import (
    AvailableResources,
    CommunityMachine,
    FleetCapability,
    PlannedTask,
)
from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest
from serverV2.orchestrator.allocation.validators.target_validator import (
    TargetValidator,
)
from serverV2.orchestrator.allocation.validators.validation_context import (
    ValidationContext,
)


class DefaultAllocationStrategy:

    def __init__(
        self,
        registry: FleetRegistry,
        validators: list[TargetValidator] | None = None,
    ) -> None:
        self._registry = registry
        self._validators: tuple[TargetValidator, ...] = tuple(validators or ())

    # ------------------------------------------------------------------
    # initial allocation
    # ------------------------------------------------------------------

    def allocate_initial(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        engine: str | None = None,
        heaviness: dict | None = None,        # ignored — pass-through for Protocol compat
        tier_budget_usd: float | None = None, # ignored — Default has no budget cap
    ) -> list[PlannedTask]:
        context = ValidationContext(engine=engine)
        targets = self._eligible_targets(resources, context)
        if not targets:
            return []

        shares = distribute_frames(
            total_frames=total_frames,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            targets=targets,
            min_frames_fn=self._min_frames_for,
        )

        tasks: list[PlannedTask] = []
        for i, share in enumerate(shares):
            tasks.append(self._share_to_task(
                share, frame_step=frame_step, chunk_index=i, attempt=0,
            ))
        return tasks

    # ------------------------------------------------------------------
    # retry — single chunk to a single target
    # ------------------------------------------------------------------

    def allocate_retry(
        self,
        chunk_request: ChunkRequest,
        resources: AvailableResources,
    ) -> PlannedTask | None:
        context = ValidationContext(engine=chunk_request.engine)
        eligible = self._eligible_targets_for_retry(resources, chunk_request, context)
        if not eligible:
            return None
        target = max(eligible, key=compute_power_score)

        if isinstance(target, CommunityMachine):
            return PlannedTask(
                fleet="community",
                machine_id=target.id,
                gpu_type=None,
                label=target.gpu_model,
                vram_gb=target.vram_gb,
                render_speed=target.render_speed,
                price_per_hour=target.price_per_hour,
                frame_start=chunk_request.frame_start,
                frame_end=chunk_request.frame_end,
                frame_step=chunk_request.frame_step,
                total_frames=chunk_request.total_frames,
                chunk_index=chunk_request.chunk_index,
                attempt=chunk_request.attempt,
            )
        # FleetCapability
        return PlannedTask(
            fleet=target.fleet,
            machine_id=None,
            gpu_type=target.gpu_type,
            label=target.label,
            vram_gb=target.vram_gb,
            render_speed=target.render_speed,
            price_per_hour=target.price_per_hour,
            frame_start=chunk_request.frame_start,
            frame_end=chunk_request.frame_end,
            frame_step=chunk_request.frame_step,
            total_frames=chunk_request.total_frames,
            chunk_index=chunk_request.chunk_index,
            attempt=chunk_request.attempt,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _eligible_targets(
        self, resources: AvailableResources, context: ValidationContext,
    ) -> list:
        """Community machines + one slot per serverless capability whose
        fleet still has headroom under ``fleet_max_parallel``, filtered
        by the strategy's TargetValidators.
        """
        community: list = []
        for m in resources.community_machines:
            if self._passes_validators(m, context):
                community.append(m)
        serverless: list = []
        in_flight = dict(resources.serverless_in_flight)
        for cap in resources.serverless_capabilities:
            if not self._registry.is_enabled(cap.fleet):
                continue
            if in_flight.get(cap.fleet, 0) >= cap.fleet_max_parallel:
                continue
            if not self._passes_validators(cap, context):
                continue
            serverless.append(cap)
        return community + serverless

    def _eligible_targets_for_retry(
        self,
        resources: AvailableResources,
        chunk_request: ChunkRequest,
        context: ValidationContext,
    ) -> list:
        excluded_caps = set(chunk_request.excluded_serverless_capabilities)
        excluded_ids = set(chunk_request.excluded_machine_ids)

        eligible = self._eligible_targets(resources, context)
        out: list = []
        for t in eligible:
            if isinstance(t, CommunityMachine):
                if t.id in excluded_ids:
                    continue
            else:  # FleetCapability
                if (t.fleet, t.gpu_type) in excluded_caps:
                    continue
            out.append(t)
        return out

    def _passes_validators(self, target, context: ValidationContext) -> bool:
        return all(v.is_valid(target, context) for v in self._validators)

    def _min_frames_for(self, target) -> int:
        if isinstance(target, CommunityMachine):
            return self._registry.min_frames_per_instance("windows")
        return self._registry.min_frames_per_instance(target.fleet)

    def _share_to_task(
        self,
        share,
        *,
        frame_step: int,
        chunk_index: int,
        attempt: int,
    ) -> PlannedTask:
        target = share.target
        if isinstance(target, CommunityMachine):
            return PlannedTask(
                fleet="community",
                machine_id=target.id,
                gpu_type=None,
                label=target.gpu_model,
                vram_gb=target.vram_gb,
                render_speed=target.render_speed,
                price_per_hour=target.price_per_hour,
                frame_start=share.frame_start,
                frame_end=share.frame_end,
                frame_step=frame_step,
                total_frames=share.total_frames,
                chunk_index=chunk_index,
                attempt=attempt,
            )
        # FleetCapability
        return PlannedTask(
            fleet=target.fleet,
            machine_id=None,
            gpu_type=target.gpu_type,
            label=target.label,
            vram_gb=target.vram_gb,
            render_speed=target.render_speed,
            price_per_hour=target.price_per_hour,
            frame_start=share.frame_start,
            frame_end=share.frame_end,
            frame_step=frame_step,
            total_frames=share.total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
        )
