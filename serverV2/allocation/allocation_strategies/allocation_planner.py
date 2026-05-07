"""AllocationPlanner -- the unified allocation algorithm.

ONE pipeline, one strategy.  The earlier Economy / Standard / Premium
shells were collapsed: cost is no longer a scoring factor (tier
semantics moved to dispatch-queue priority).  Per-offer ranking
(individual Vast offers, each with its own price / CUDA / OS) replaced
per-gpu_type aggregation.

Pipeline (``plan_initial``):

  1. Validators + VRAM feasibility filter
       required = estimate_required_vram_gb(heaviness)
       eligible = [t for t in resources if t.vram_gb >= required * weights.vram_safety_factor]
       fall back to "drop the floor" if empty -- better to try than refuse.

  2. Knapsack-aware mix size (the K-decision)
       Per-chunk render time should be at least
       ``weights.startup_amortization_ratio`` * startup, otherwise a big
       chunk of total spend goes to overhead.  Cap K against
       ``max_targets`` and ``total_frames // min_frames_per_chunk``.

         total_render_sec = total_frames * representative_spf
         worst_startup    = baseline_startup + max(fleet_buffer)
         K_max_amortized  = total_render_sec / (worst_startup * ratio)
         K = min(max_targets, K_max_amortized, total_frames / min_frames_per_chunk)

  3. Score every eligible target
       composite_scorer.score_target(...) returns
       speed * cuda * os linear combination (no cost).  Per-target
       fleet buffer applied to chunk_seconds.

  4. Pick top-N with fleet + per-gpu_type diversification.

  5. Time-balanced frame distribution.

  6. Stamp estimate fields onto every PlannedTask.

Retry path (``plan_retry``) uses the same scorer with a single-target
return + anti-affinity exclusions.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Any

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.allocation_strategies.allocation_weights import (
    AllocationWeights,
)
from serverV2.allocation.allocation_strategies.analyzers.allocation_composite_scorer import (
    chunk_cost_for,
    chunk_seconds_for,
    score_target,
)
from serverV2.allocation.allocation_strategies.analyzers.allocation_time_analyzer import (
    estimate_seconds_per_frame,
    estimate_startup_seconds,
)
from serverV2.allocation.allocation_strategies.analyzers.allocation_vram_estimator import (
    estimate_required_vram_gb,
)
from serverV2.allocation.allocation_strategies.validators.allocation_target_validator import (
    AllocationTargetValidator,
)
from serverV2.allocation.allocation_strategies.validators.allocation_validation_context import (
    AllocationValidationContext,
)
from serverV2.config import StartupBufferConfig, VramFleetBoostConfig
from serverV2.core.models import (
    AvailableResources,
    CommunityMachine,
    FleetCapability,
    PlannedTask,
)
from serverV2.fleets.registry import FleetRegistry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ScoredTarget:
    target: Any
    score: float
    fleet_key: str        # "community" or fleet name -- for fleet diversification cap
    gpu_type_key: str     # "(fleet,gpu_type)" or "" for community -- for per-gpu-type cap


class AllocationPlanner:

    def __init__(
        self,
        registry: FleetRegistry,
        validators: list[AllocationTargetValidator] | None = None,
    ) -> None:
        # Phase 2b: planner is pure-functional in its config inputs --
        # callers pass weights / startup_buffer_sec / vram_fleet_boost
        # at each plan call.  AllocationPlanningService is the single
        # config reader and feeds those slices in.  No repo on the
        # planner.
        self._registry = registry
        self._validators: tuple[AllocationTargetValidator, ...] = tuple(validators or ())

    # ------------------------------------------------------------------
    # initial allocation
    # ------------------------------------------------------------------

    def plan_initial(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        weights: AllocationWeights,
        startup_buffer_sec: StartupBufferConfig,
        vram_fleet_boost: VramFleetBoostConfig,
        engine: str | None = None,
        heaviness: dict | None = None,
    ) -> list[PlannedTask]:
        if total_frames <= 0:
            return []

        # Phase 2b: config slices arrive via params from the planning
        # service (the single config reader).  Pure-functional inputs.
        startup_buffer = startup_buffer_sec
        vram_boost = vram_fleet_boost

        heaviness = heaviness or {}
        context = AllocationValidationContext(engine=engine)

        # Step 1: validators + VRAM feasibility filter
        eligible = self._eligible_targets(resources, heaviness, weights, vram_boost, context)
        if not eligible:
            # Drop the VRAM floor entirely -- still better to try than refuse
            eligible = self._eligible_targets_no_vram(resources, vram_boost, context)
        if not eligible:
            return []

        # Step 2: knapsack-aware mix size.
        # max_targets is an upper bound, NOT a goal.  For each render we
        # pick K such that per-chunk render time is meaningfully larger
        # than per-chunk startup; otherwise a big chunk of total compute
        # is wasted on overhead.  The amortization ratio (default 0.5)
        # says "render >= 50% of startup".  Worst-case startup uses the
        # largest fleet buffer (Vast) since at K-decision time we don't
        # yet know which fleet will get the chunk.
        spfs = [
            estimate_seconds_per_frame(heaviness, t.render_speed)
            for t in eligible
        ]
        median_spf = max(0.001, median(spfs)) if spfs else 1.0
        worst_startup = (
            estimate_startup_seconds(heaviness)
            + max(startup_buffer.vast,
                  startup_buffer.modal,
                  startup_buffer.community)
        )
        ratio = max(0.05, weights.startup_amortization_ratio)
        total_render_sec = total_frames * median_spf
        k_amortized = max(1, math.ceil(total_render_sec / (worst_startup * ratio)))
        max_by_frames = max(1, total_frames // weights.min_frames_per_chunk)
        ideal_count = min(weights.max_targets, k_amortized, max_by_frames, len(eligible))
        if ideal_count <= 0:
            return []
        even_chunk = max(1, total_frames // ideal_count)

        log.info(
            "[ALLOC] knapsack: total_frames=%d median_spf=%.1fs worst_startup=%.0fs"
            " ratio=%.2f k_amortized=%d max_by_frames=%d eligible=%d -> K=%d",
            total_frames, median_spf, worst_startup, ratio,
            k_amortized, max_by_frames, len(eligible), ideal_count,
        )

        # Step 3: score every eligible target with composite scorer
        scored = [
            _ScoredTarget(
                target=t,
                score=score_target(
                    render_speed=t.render_speed,
                    heaviness=heaviness,
                    estimated_chunk_frames=even_chunk,
                    weights=weights,
                    cuda_version=_target_cuda(t),
                    host_os=_target_host_os(t),
                    engine=engine,
                    fleet=_target_fleet(t),
                    fleet_buffer_sec=_buffer_for_target(startup_buffer, t),
                ),
                fleet_key=_fleet_key(t),
                gpu_type_key=_gpu_type_key(t),
            )
            for t in eligible
        ]
        scored.sort(key=lambda s: -s.score)

        # Step 4: pick top-N with soft fleet diversification
        selected = self._select_with_diversification(scored, ideal_count, weights)
        if not selected:
            return []

        # Step 5: time-balanced frame distribution
        shares = self._distribute_time_balanced(
            targets=[s.target for s in selected],
            heaviness=heaviness,
            total_frames=total_frames,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
        )

        # Step 6: build PlannedTasks with estimate fields stamped
        return [
            self._target_to_task(
                startup_buffer=startup_buffer,
                target=share.target,
                heaviness=heaviness,
                frame_start=share.frame_start,
                frame_end=share.frame_end,
                frame_step=frame_step,
                total_frames=share.total_frames,
                chunk_index=i,
                attempt=0,
            )
            for i, share in enumerate(shares)
        ]

    # ------------------------------------------------------------------
    # retry -- single chunk to the highest-scoring eligible target
    # ------------------------------------------------------------------

    def plan_retry(
        self,
        chunk_request: AllocationChunkRequest,
        resources: AvailableResources,
        *,
        weights: AllocationWeights,
        startup_buffer_sec: StartupBufferConfig,
        vram_fleet_boost: VramFleetBoostConfig,
        heaviness: dict | None = None,
    ) -> PlannedTask | None:
        # Phase 2b: pure-functional in config inputs.
        startup_buffer = startup_buffer_sec
        vram_boost = vram_fleet_boost

        heaviness = heaviness or {}
        context = AllocationValidationContext(engine=chunk_request.engine)

        eligible = self._eligible_targets(resources, heaviness, weights, vram_boost, context)
        if not eligible:
            eligible = self._eligible_targets_no_vram(resources, vram_boost, context)

        excluded_caps = set(chunk_request.excluded_serverless_capabilities)
        excluded_ids = set(chunk_request.excluded_machine_ids)
        eligible = [
            t for t in eligible
            if (
                t.id not in excluded_ids if isinstance(t, CommunityMachine)
                else (t.fleet, t.gpu_type) not in excluded_caps
            )
        ]
        if not eligible:
            return None

        # Score each eligible target.  Same scoring + selection pipeline
        # as plan_initial -- using the shared selector means rules like
        # "community-first" live in exactly one place
        # (_select_with_diversification) and apply consistently to both
        # initial planning and retry dispatch.
        chunk_frames = chunk_request.total_frames
        scored = [
            _ScoredTarget(
                target=t,
                score=score_target(
                    render_speed=t.render_speed,
                    heaviness=heaviness,
                    estimated_chunk_frames=chunk_frames,
                    weights=weights,
                    cuda_version=_target_cuda(t),
                    host_os=_target_host_os(t),
                    engine=chunk_request.engine,
                    fleet=_target_fleet(t),
                    fleet_buffer_sec=_buffer_for_target(startup_buffer, t),
                ),
                fleet_key=_fleet_key(t),
                gpu_type_key=_gpu_type_key(t),
            )
            for t in eligible
        ]
        scored.sort(key=lambda s: -s.score)
        selected = self._select_with_diversification(scored, 1, weights)
        if not selected:
            return None
        best = selected[0].target
        return self._target_to_task(
            startup_buffer=startup_buffer,
            target=best,
            heaviness=heaviness,
            frame_start=chunk_request.frame_start,
            frame_end=chunk_request.frame_end,
            frame_step=chunk_request.frame_step,
            total_frames=chunk_frames,
            chunk_index=chunk_request.chunk_index,
            attempt=chunk_request.attempt,
        )

    # ------------------------------------------------------------------
    # eligibility helpers
    # ------------------------------------------------------------------

    def _eligible_targets(
        self,
        resources: AvailableResources,
        heaviness: dict,
        weights: AllocationWeights,
        vram_boost: VramFleetBoostConfig,
        context: AllocationValidationContext,
    ) -> list:
        required_vram = estimate_required_vram_gb(heaviness) * weights.vram_safety_factor
        return self._collect_eligibles(resources, vram_boost, required_vram, context)

    def _eligible_targets_no_vram(
        self,
        resources: AvailableResources,
        vram_boost: VramFleetBoostConfig,
        context: AllocationValidationContext,
    ) -> list:
        return self._collect_eligibles(resources, vram_boost, 0.0, context)

    def _collect_eligibles(
        self,
        resources: AvailableResources,
        vram_boost: VramFleetBoostConfig,
        vram_floor: float,
        context: AllocationValidationContext,
    ) -> list:
        out: list = []
        community_boost = vram_boost.for_fleet("community")
        for m in resources.community_machines:
            if m.vram_gb * community_boost < vram_floor:
                continue
            if not self._passes_validators(m, context):
                continue
            out.append(m)
        in_flight = resources.serverless_in_flight
        for cap in resources.serverless_capabilities:
            if not self._registry.is_enabled(cap.fleet):
                continue
            if cap.vram_gb * vram_boost.for_fleet(cap.fleet) < vram_floor:
                continue
            if not self._passes_validators(cap, context):
                continue
            headroom = max(
                0, cap.fleet_max_parallel - in_flight.get(cap.fleet, 0),
            )
            if headroom <= 0:
                continue
            if cap.offer_id is not None:
                # Vast (per-offer): each offer is exactly one rentable
                # instance.  Append once.  Total slots per fleet =
                # number of offers, naturally bounded by the live
                # marketplace pool.
                out.append(cap)
            else:
                # Modal / serverless-without-offer-id: one capability
                # represents "I can spawn N of these gpu_type".
                # Materialize up to ``headroom`` virtual slots per
                # gpu_type so the picker can take multiple chunks of
                # the same class.  Total may exceed fleet max_parallel
                # across gpu_types -- dispatch-time fleet cap enforces
                # the absolute ceiling.
                for _ in range(headroom):
                    out.append(cap)
        return out

    def _passes_validators(self, target, context: AllocationValidationContext) -> bool:
        return all(v.is_valid(target, context) for v in self._validators)

    # ------------------------------------------------------------------
    # mix selection -- score-sorted with soft fleet diversification
    # ------------------------------------------------------------------

    @staticmethod
    def _select_with_diversification(
        scored: list[_ScoredTarget],
        max_picks: int,
        weights: AllocationWeights,
    ) -> list[_ScoredTarget]:
        """Community-first selection with diversification on the
        serverless remainder.

        Community machines are user-owned hardware that's already paid
        for and idle.  Whenever any community machine is eligible, ALL
        eligible community machines are taken first (in score order
        among themselves) before any serverless capability is picked.
        The diversification cap below does NOT apply to community --
        the cap was meant to stop a single serverless fleet/GPU class
        from monopolising picks; community machines are individually
        owned with no monopoly concern.

        Diversification on the remaining (serverless) slots:

          * fleet_cap  -- no single serverless fleet (vast / modal) may
                          hold more than ``fleet_diversification_cap``
                          fraction of the serverless slots
          * gpu_cap    -- no single ``(fleet, gpu_type)`` may hold more
                          than ``gpu_type_diversification_cap`` fraction
                          of the serverless slots

        Targets that bust either cap go to the rejected pool and are
        backfilled in score order if the soft caps left us under the
        remaining slot count.  Both caps are floors-of-1 so a tiny
        render that fits in one target still allocates.

        Used by both ``plan_initial`` (K = ideal_count) and
        ``plan_retry`` (K = 1) so the rule lives in exactly one place.
        """
        # Pass 1: take every community machine, in score order, up to
        # max_picks.  Community is exempt from the diversification cap.
        community_taken: set[str] = set()
        community_picks: list[_ScoredTarget] = []
        serverless_scored: list[_ScoredTarget] = []
        for s in scored:
            t = s.target
            if isinstance(t, CommunityMachine):
                if t.id in community_taken:
                    continue
                if len(community_picks) < max_picks:
                    community_picks.append(s)
                    community_taken.add(t.id)
            else:
                serverless_scored.append(s)

        remaining = max_picks - len(community_picks)
        if remaining <= 0:
            return community_picks

        # Pass 2: existing diversification logic on the serverless slice.
        fleet_cap = max(1, int(remaining * weights.fleet_diversification_cap))
        gpu_cap = max(1, int(remaining * weights.gpu_type_diversification_cap))

        selected: list[_ScoredTarget] = []
        rejected: list[_ScoredTarget] = []
        fleet_counts: dict[str, int] = defaultdict(int)
        gpu_counts: dict[str, int] = defaultdict(int)

        for s in serverless_scored:
            if len(selected) >= remaining:
                break
            if fleet_counts[s.fleet_key] >= fleet_cap:
                rejected.append(s)
                continue
            if s.gpu_type_key and gpu_counts[s.gpu_type_key] >= gpu_cap:
                rejected.append(s)
                continue
            selected.append(s)
            fleet_counts[s.fleet_key] += 1
            if s.gpu_type_key:
                gpu_counts[s.gpu_type_key] += 1

        # Backfill from the rejected pool, score-order preserved.
        # Backfill ignores both caps -- the picker already preferred
        # diversified picks; if we're still short, take what's left.
        for s in rejected:
            if len(selected) >= remaining:
                break
            selected.append(s)

        return community_picks + selected

    # ------------------------------------------------------------------
    # frame distribution -- time-balanced (inverse-proportional to spf)
    # ------------------------------------------------------------------

    @dataclass(frozen=True)
    class _Share:
        target: Any
        frame_start: int
        frame_end: int
        total_frames: int

    @staticmethod
    def _distribute_time_balanced(
        targets: list,
        heaviness: dict,
        total_frames: int,
        frame_start: int,
        frame_end: int,
        frame_step: int,
    ) -> list["AllocationPlanner._Share"]:
        """Distribute frames so each chunk's wall time is as equal as
        possible.  Faster GPUs get more frames; the slowest GPU doesn't
        bottleneck the render.  Minimises both wall time and total cost.
        """
        if not targets or total_frames <= 0:
            return []

        # Compute per-frame seconds for each target on this scene.  Lower
        # spf = faster.  Frame share is inversely proportional.
        spfs = [
            max(0.001, estimate_seconds_per_frame(heaviness, t.render_speed))
            for t in targets
        ]
        weights = [1.0 / s for s in spfs]
        total_w = sum(weights)
        if total_w <= 0:
            # Pathological -- fall back to even split
            weights = [1.0] * len(targets)
            total_w = float(len(targets))

        # Allocate integer frame counts, rounding down then distributing
        # the residual to the highest-weight slots.
        raw = [w / total_w * total_frames for w in weights]
        counts = [int(r) for r in raw]
        residual = total_frames - sum(counts)
        if residual > 0:
            # Give the residual frames to the highest-weight slots.
            order = sorted(range(len(weights)), key=lambda i: -weights[i])
            for i in order[:residual]:
                counts[i] += 1

        # Build shares walking the frame range in order of `targets`.
        shares: list[AllocationPlanner._Share] = []
        current = frame_start
        for i, (target, count) in enumerate(zip(targets, counts)):
            if count <= 0:
                continue
            chunk_end = current + (count - 1) * frame_step
            # Last share clamped exactly to frame_end to avoid step drift
            if i == len(targets) - 1:
                chunk_end = frame_end
            shares.append(AllocationPlanner._Share(
                target=target,
                frame_start=current,
                frame_end=chunk_end,
                total_frames=count,
            ))
            current = chunk_end + frame_step

        return shares

    # ------------------------------------------------------------------
    # PlannedTask construction with estimate stamping
    # ------------------------------------------------------------------

    def _target_to_task(
        self,
        *,
        startup_buffer: StartupBufferConfig,
        target,
        heaviness: dict,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        chunk_index: int | None,
        attempt: int,
    ) -> PlannedTask:
        fleet_buffer = _buffer_for_target(startup_buffer, target)
        spf = estimate_seconds_per_frame(heaviness, target.render_speed)
        startup = estimate_startup_seconds(heaviness) + fleet_buffer
        seconds = chunk_seconds_for(
            render_speed=target.render_speed,
            heaviness=heaviness,
            estimated_chunk_frames=total_frames,
            fleet_buffer_sec=fleet_buffer,
        )
        cost = chunk_cost_for(
            render_speed=target.render_speed,
            price_per_hour=target.price_per_hour,
            heaviness=heaviness,
            estimated_chunk_frames=total_frames,
            fleet_buffer_sec=fleet_buffer,
        )

        if isinstance(target, CommunityMachine):
            return PlannedTask(
                fleet="community",
                machine_id=target.id,
                gpu_type=None,
                label=target.gpu_model,
                vram_gb=target.vram_gb,
                render_speed=target.render_speed,
                price_per_hour=target.price_per_hour,
                frame_start=frame_start,
                frame_end=frame_end,
                frame_step=frame_step,
                total_frames=total_frames,
                chunk_index=chunk_index,
                attempt=attempt,
                estimated_seconds=seconds,
                estimated_cost_usd=cost,
                estimated_seconds_per_frame=spf,
                estimated_startup_seconds=startup,
            )
        # FleetCapability -- serverless
        return PlannedTask(
            fleet=target.fleet,
            machine_id=None,
            gpu_type=target.gpu_type,
            label=target.label,
            vram_gb=target.vram_gb,
            render_speed=target.render_speed,
            price_per_hour=target.price_per_hour,
            offer_id=target.offer_id,
            cuda_version=target.cuda_version,
            host_os=target.host_os,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
            estimated_seconds=seconds,
            estimated_cost_usd=cost,
            estimated_seconds_per_frame=spf,
            estimated_startup_seconds=startup,
        )


def _fleet_key(target) -> str:
    """The fleet-level diversification cap groups every community
    machine under one 'community' bucket and each serverless fleet
    under its own bucket (vast_serverless / modal_serverless).
    Keeps a single fleet from dominating the mix.
    """
    if isinstance(target, CommunityMachine):
        return "community"
    if isinstance(target, FleetCapability):
        return target.fleet
    return "unknown"


def _gpu_type_key(target) -> str:
    """The per-gpu-type cap key.  Empty for community machines (they're
    already unique by machine_id); ``"<fleet>:<gpu_type>"`` for
    serverless, so 'vast_serverless:RTX 4080S' is distinct from
    'modal_serverless:RTX 4080S' (different supply pools).
    """
    if isinstance(target, FleetCapability):
        return f"{target.fleet}:{target.gpu_type}"
    return ""


def _target_fleet(target) -> str | None:
    if isinstance(target, CommunityMachine):
        return "community"
    if isinstance(target, FleetCapability):
        return target.fleet
    return None


def _target_cuda(target) -> str | None:
    """Per-target CUDA version, or None if untracked.  Community
    machines and Modal capabilities always None for now.  Vast
    capabilities carry their offer's ``cuda_max_good`` string.
    """
    if isinstance(target, FleetCapability):
        return target.cuda_version
    return None


def _target_host_os(target) -> str | None:
    """Per-target host OS, or None if untracked.  Same shape as
    ``_target_cuda`` -- only Vast capabilities populate this today.
    """
    if isinstance(target, FleetCapability):
        return target.host_os
    return None


# Module-level helper: per-target fleet buffer lookup.  Phase 2 dropped
# the bound-method form because the planner no longer holds a frozen
# StartupBufferConfig -- callers pull the fresh instance from cfg at the
# top of each plan call and pass it through.
def _buffer_for_target(startup_buffer: StartupBufferConfig, target) -> float:
    return startup_buffer.for_fleet(_target_fleet(target))
