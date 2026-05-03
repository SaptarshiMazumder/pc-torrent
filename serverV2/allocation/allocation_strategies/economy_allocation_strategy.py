"""EconomyAllocationStrategy — cost-first allocator (Phase 6).

Picks the cheapest targets first, with soft fleet diversification (no
single fleet dominates).  Caps at 6 distinct targets, distributes frames
evenly (not speed-weighted — Economy doesn't optimize wall time).

Used by the "Economy" tier (see tiered_allocation_plan.md).  Wired into
``RenderLifecycle._pick_strategy`` in Phase 8 (tier selection).  In
Phase 6 the class is created and smoke-tested in isolation; nothing
calls it from the live dispatch path yet.

Algorithm (allocate_initial):

  1. Build eligibles
     - community machines + ``headroom`` virtual slots per serverless
       capability (so a single capability with capacity for 3 contributes
       3 candidate slots)
     - filter via TargetValidators (engine compat — same as today)
     - apply VRAM floor from heaviness band (shared with FastRender)
     - if VRAM floor empties the list, drop the floor and try again

  2. Sort cheapest-first
     primary: price_per_hour ASC
     tiebreaker: render_speed DESC (free wall-time win for same price)

  3. Soft diversification pick
     fleet_cap = max(1, floor(ECONOMY_HARD_CAP * 0.70))   # 4 for cap=6
     - skip targets whose fleet already has fleet_cap picks
     - rejected go to a backfill list
     - if diversification leaves us short, backfill from rejected
       (cheapest-first) — the cap is a *preference*, not a *constraint*

  4. Trim by min(ECONOMY_HARD_CAP, total_frames) — never empty chunks

  5. Distribute frames evenly via the shared ``distribute_frames`` helper

allocate_retry: cheapest single target after anti-affinity filter.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from serverV2.core.models import (
    AvailableResources,
    CommunityMachine,
    FleetCapability,
    PlannedTask,
)
from serverV2.fleets.registry import FleetRegistry
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import AllocationChunkRequest
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_heaviness_bands import vram_floor_for
from serverV2.allocation.allocation_strategies.validators.allocation_target_validator import (
    AllocationTargetValidator,
)
from serverV2.allocation.allocation_strategies.validators.allocation_validation_context import (
    AllocationValidationContext,
)


# Max distinct targets in an Economy mix.  Bigger chunks per machine,
# longer wall time, lower cost — balanced against per-chunk startup tax.
ECONOMY_HARD_CAP = 6

# Soft fleet diversification cap — fraction of total picks allowed from
# any single fleet.  Backfills from rejected pool when one fleet
# dominates the eligibles, so this is a *preference* not a *constraint*.
ECONOMY_FLEET_DIVERSIFICATION_CAP = 0.70

# Per-chunk minimum frame count.  Economy prefers fewer-bigger-chunks so
# the per-chunk startup tax doesn't dominate.  ``max_picks`` is bounded
# by ``total_frames // ECONOMY_MIN_FRAMES_PER_CHUNK`` (with a floor of 1).
ECONOMY_MIN_FRAMES_PER_CHUNK = 3


@dataclass(frozen=True)
class _Share:
    """Internal frame-share record for the even distributor.  Mirrors
    ``FrameShare`` from the shared distributor without importing it (we
    don't want the speed-weighting / re-sorting baggage)."""
    target: Any
    frame_start: int
    frame_end: int
    total_frames: int


class EconomyAllocationStrategy:

    def __init__(
        self,
        registry: FleetRegistry,
        validators: list[AllocationTargetValidator] | None = None,
    ) -> None:
        self._registry = registry
        self._validators: tuple[AllocationTargetValidator, ...] = tuple(validators or ())

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
        heaviness: dict | None = None,
        tier_budget_usd: float | None = None, # ignored — Economy is already cost-first
    ) -> list[PlannedTask]:
        context = AllocationValidationContext(engine=engine)
        # Heaviness carries file_size_bytes (Phase 7-refactor consolidation).
        # None heaviness -> file_size 0 -> medium-band default.
        file_size_bytes = int((heaviness or {}).get("file_size_bytes", 0) or 0)
        vram_floor = vram_floor_for(file_size_bytes)

        eligible = self._eligible_targets(resources, vram_floor, context)
        if not eligible:
            # Drop the VRAM floor rather than refuse — better to try a
            # too-small card than fail to allocate at all.  Matches
            # FastRender's degradation behaviour.
            eligible = self._eligible_targets(resources, 0, context)
        if not eligible:
            return []

        # Cheapest first, render_speed as tiebreaker (faster wins among
        # same-price — free wall-time improvement, no cost penalty).
        eligible.sort(key=lambda t: (t.price_per_hour, -t.render_speed))

        # Few-bigger-chunks: cap by both ECONOMY_HARD_CAP and the
        # minimum-chunk-size guard.  3 frames -> 1 machine; 18+ -> 6.
        max_picks = min(
            ECONOMY_HARD_CAP,
            max(1, total_frames // ECONOMY_MIN_FRAMES_PER_CHUNK),
        )
        if max_picks <= 0 or total_frames <= 0:
            return []

        selected = self._select_with_soft_diversification(eligible, max_picks)
        if not selected:
            return []

        shares = self._distribute_evenly(
            targets=selected,
            total_frames=total_frames,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
        )

        tasks: list[PlannedTask] = []
        for i, share in enumerate(shares):
            tasks.append(self._target_to_task(
                share.target,
                frame_start=share.frame_start,
                frame_end=share.frame_end,
                frame_step=frame_step,
                total_frames=share.total_frames,
                chunk_index=i,
                attempt=0,
            ))
        return tasks

    # ------------------------------------------------------------------
    # retry — single chunk, anti-affinity-respecting, cheapest pick
    # ------------------------------------------------------------------

    def allocate_retry(
        self,
        chunk_request: AllocationChunkRequest,
        resources: AvailableResources,
    ) -> PlannedTask | None:
        context = AllocationValidationContext(engine=chunk_request.engine)
        vram_floor = vram_floor_for(chunk_request.file_size_bytes)
        eligible = self._eligible_targets(resources, vram_floor, context)
        if not eligible:
            eligible = self._eligible_targets(resources, 0, context)

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

        target = min(
            eligible,
            key=lambda t: (t.price_per_hour, -t.render_speed),
        )
        return self._target_to_task(
            target,
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
        self,
        resources: AvailableResources,
        vram_gb_min: float,
        context: AllocationValidationContext,
    ) -> list:
        out: list = []
        for m in resources.community_machines:
            if m.vram_gb < vram_gb_min:
                continue
            if not self._passes_validators(m, context):
                continue
            out.append(m)
        in_flight = resources.serverless_in_flight
        for cap in resources.serverless_capabilities:
            if not self._registry.is_enabled(cap.fleet):
                continue
            headroom = cap.fleet_max_parallel - in_flight.get(cap.fleet, 0)
            if headroom <= 0:
                continue
            if cap.vram_gb < vram_gb_min:
                continue
            if not self._passes_validators(cap, context):
                continue
            # One virtual slot per available headroom — each slot is a
            # candidate the cheapest-first picker can take or skip.
            for _ in range(headroom):
                out.append(cap)
        return out

    def _passes_validators(self, target, context: AllocationValidationContext) -> bool:
        return all(v.is_valid(target, context) for v in self._validators)

    @staticmethod
    def _select_with_soft_diversification(
        sorted_eligible: list,
        max_picks: int,
    ) -> list:
        """Greedy cheapest-first with a soft per-fleet cap.

        Walks ``sorted_eligible`` once; skips targets when their fleet
        already has ``fleet_cap`` picks.  Rejected targets go to a
        backfill list and are pulled in cheapest-first if diversification
        leaves us under ``max_picks``.

        Community machines are unique by id — taking the same machine
        twice would be a bug.  Serverless capabilities can be taken
        multiple times (they represent fungible slots up to fleet_cap).
        """
        fleet_cap = max(1, int(max_picks * ECONOMY_FLEET_DIVERSIFICATION_CAP))
        selected: list = []
        rejected: list = []
        fleet_counts: dict[str, int] = defaultdict(int)
        community_taken: set[str] = set()

        def _key(target) -> str:
            if isinstance(target, CommunityMachine):
                return "community"
            return target.fleet

        for target in sorted_eligible:
            if len(selected) >= max_picks:
                break
            if isinstance(target, CommunityMachine) and target.id in community_taken:
                continue
            f = _key(target)
            if fleet_counts[f] >= fleet_cap:
                rejected.append(target)
                continue
            selected.append(target)
            if isinstance(target, CommunityMachine):
                community_taken.add(target.id)
            fleet_counts[f] += 1

        # Backfill from rejected if diversification left us short.
        for target in rejected:
            if len(selected) >= max_picks:
                break
            if isinstance(target, CommunityMachine) and target.id in community_taken:
                continue
            selected.append(target)
            if isinstance(target, CommunityMachine):
                community_taken.add(target.id)

        return selected

    @staticmethod
    def _distribute_evenly(
        targets: list,
        total_frames: int,
        frame_start: int,
        frame_end: int,
        frame_step: int,
    ) -> list[_Share]:
        """Even split — each target gets ``total_frames // n`` frames; the
        first ``total_frames % n`` targets get one extra each.

        Preserves target order from the picker (cheapest-first) — does
        NOT re-sort by speed.  No min-frames-per-target check; that's
        handled upstream by ``max_picks`` capping at
        ``total_frames // ECONOMY_MIN_FRAMES_PER_CHUNK``.
        """
        if not targets or total_frames <= 0:
            return []
        n = len(targets)
        base = total_frames // n
        residual = total_frames % n

        shares: list[_Share] = []
        current = frame_start
        for i, target in enumerate(targets):
            chunk = base + (1 if i < residual else 0)
            if chunk == 0:
                continue
            chunk_end = current + (chunk - 1) * frame_step
            # Last share clamped exactly to frame_end to avoid step drift.
            if i == n - 1:
                chunk_end = frame_end
            shares.append(_Share(
                target=target,
                frame_start=current,
                frame_end=chunk_end,
                total_frames=chunk,
            ))
            current = chunk_end + frame_step
        return shares

    @staticmethod
    def _target_to_task(
        target,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        chunk_index: int | None,
        attempt: int,
    ) -> PlannedTask:
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
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
        )


# ---------------------------------------------------------------------------
# Smoke test — `python -m serverV2.allocation.allocation_strategies.economy_allocation_strategy`
# ---------------------------------------------------------------------------

def _smoke() -> None:
    """Hard-asserted algorithm checks for EconomyAllocationStrategy.

    No DB, no network — uses plain dataclass instances and a stub registry.
    """
    print("=== EconomyAllocationStrategy smoke test ===\n")

    # ----- Stub registry -----
    class _StubRegistry:
        def __init__(self, enabled_fleets: set[str]) -> None:
            self._enabled = enabled_fleets

        def is_enabled(self, fleet: str) -> bool:
            return fleet in self._enabled

        def min_frames_per_instance(self, fleet: str) -> int:
            return {
                "community": 2,
                "modal_serverless": 5,
                "vast_serverless": 10,
            }.get(fleet, 1)

    registry = _StubRegistry({"community", "modal_serverless", "vast_serverless"})

    # ----- Helper builders -----
    def _community(id_: str, vram: float = 24.0, speed: float = 1.0, price: float = 1.0) -> CommunityMachine:
        return CommunityMachine(
            id=id_, gpu_model=f"GPU-{id_}",
            vram_gb=vram, cpu_cores=8, ram_gb=32,
            render_speed=speed, status="idle", last_seen_at=None,
            price_per_hour=price,
        )

    def _vast_cap(gpu: str, vram: float = 24.0, speed: float = 1.0, price: float = 0.45,
                  max_par: int = 15) -> FleetCapability:
        return FleetCapability(
            fleet="vast_serverless", gpu_type=gpu, label=f"Vast {gpu}",
            vram_gb=vram, cpu_cores=8, ram_gb=32,
            render_speed=speed, fleet_max_parallel=max_par, price_per_hour=price,
        )

    def _modal_cap(gpu: str, vram: float = 24.0, speed: float = 1.0, price: float = 1.10,
                   max_par: int = 15) -> FleetCapability:
        return FleetCapability(
            fleet="modal_serverless", gpu_type=gpu, label=f"Modal {gpu}",
            vram_gb=vram, cpu_cores=8, ram_gb=32,
            render_speed=speed, fleet_max_parallel=max_par, price_per_hour=price,
        )

    def _resources(community: list, caps: list, in_flight: dict | None = None) -> AvailableResources:
        return AvailableResources(
            community_machines=community,
            serverless_capabilities=caps,
            serverless_in_flight=in_flight or {},
        )

    strategy = EconomyAllocationStrategy(registry)

    # ----- 1. Cheapest-first across mixed fleets -----
    # 30 frames -> max_picks = min(6, 30//3) = 6; only 3 eligibles -> all 3 picked.
    res = _resources(
        community=[_community("c1", price=0.20)],
        caps=[
            _vast_cap("RTX 4090", price=0.45, max_par=1),
            _modal_cap("h100", price=4.50, max_par=1),
        ],
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=30, frame_step=1, total_frames=30,
        resources=res,
    )
    fleets = [t.fleet for t in tasks]
    assert len(tasks) == 3, f"expected 3 tasks, got {len(tasks)}"
    assert "community" in fleets         # cheapest at $0.20
    assert "vast_serverless" in fleets   # next at $0.45
    assert "modal_serverless" in fleets  # last at $4.50
    print(f"1. mixed fleets cheapest-first: 30 frames -> {fleets}   OK")

    # ----- 2. Cap at ECONOMY_HARD_CAP=6 distinct targets -----
    res = _resources(
        community=[_community(f"c{i}", price=0.10 + i * 0.01) for i in range(20)],
        caps=[],
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=100, frame_step=1, total_frames=100,
        resources=res,
    )
    assert len(tasks) == ECONOMY_HARD_CAP, f"expected {ECONOMY_HARD_CAP} tasks, got {len(tasks)}"
    print(f"2. cap at {ECONOMY_HARD_CAP} distinct targets: 20 eligibles -> {len(tasks)} tasks   OK")

    # ----- 3. Trimmed by ECONOMY_MIN_FRAMES_PER_CHUNK -----
    # 7 frames / 3-min = 2 picks (with 1 residual frame in the first pick)
    res = _resources(
        community=[_community(f"c{i}", price=0.10 + i * 0.01) for i in range(10)],
        caps=[],
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=7, frame_step=1, total_frames=7,
        resources=res,
    )
    assert len(tasks) == 2, f"expected 2 tasks (7 frames / 3 min = 2), got {len(tasks)}"
    chunk_sizes = sorted([t.total_frames for t in tasks], reverse=True)
    assert sum(chunk_sizes) == 7
    assert chunk_sizes == [4, 3], f"expected [4,3], got {chunk_sizes}"
    print(f"3. min-frames-per-chunk guard: 7 frames -> {len(tasks)} tasks {chunk_sizes}    OK")

    # ----- 4. Diversification cap kicks in -----
    # 10 vast eligibles + 5 modal — fleet_cap = floor(6 * 0.70) = 4
    # Expect: 4 vast + 2 modal
    res = _resources(
        community=[],
        caps=[
            _vast_cap("RTX 4090", price=0.30, max_par=15),
            _modal_cap("l4", price=1.10, max_par=10),
        ],
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=100, frame_step=1, total_frames=100,
        resources=res,
    )
    fleets = [t.fleet for t in tasks]
    vast_count = sum(1 for f in fleets if f == "vast_serverless")
    modal_count = sum(1 for f in fleets if f == "modal_serverless")
    assert len(tasks) == ECONOMY_HARD_CAP, f"expected {ECONOMY_HARD_CAP} tasks"
    assert vast_count == 4, f"expected 4 vast (fleet cap), got {vast_count}"
    assert modal_count == 2, f"expected 2 modal (backfill), got {modal_count}"
    print(f"4. diversification cap: 4 vast + 2 modal (cap=4 per fleet)              OK")

    # ----- 5. Diversification backfill: only one fleet available -----
    # 10 vast eligibles, no other fleet -> all 6 from vast (soft cap degraded)
    res = _resources(
        community=[],
        caps=[_vast_cap("RTX 4090", price=0.30, max_par=15)],
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=100, frame_step=1, total_frames=100,
        resources=res,
    )
    fleets = [t.fleet for t in tasks]
    assert len(tasks) == ECONOMY_HARD_CAP
    assert all(f == "vast_serverless" for f in fleets), \
        "all 6 should be vast since no other fleet available"
    print(f"5. backfill when one fleet dominates: 6 vast (soft cap degrades)        OK")

    # ----- 6. VRAM floor filtering -----
    # 3 GB file -> band -> vram_floor=24.  8/16 GB targets filtered out.
    res = _resources(
        community=[
            _community("c8gb", vram=8, price=0.05),
            _community("c16gb", vram=16, price=0.10),
            _community("c24gb", vram=24, price=0.20),
            _community("c48gb", vram=48, price=0.30),
        ],
        caps=[],
    )
    from serverV2.core.value_objects import parse_analysis_heaviness
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=10, frame_step=1, total_frames=10,
        resources=res,
        heaviness=parse_analysis_heaviness(None, file_size_bytes=3 * 1024 ** 3),
    )
    machine_ids = [t.machine_id for t in tasks]
    assert "c8gb" not in machine_ids and "c16gb" not in machine_ids
    assert "c24gb" in machine_ids and "c48gb" in machine_ids
    print(f"6. VRAM floor for 3GB file (24GB floor): {sorted(machine_ids)}        OK")

    # ----- 7. VRAM floor fallback when no eligible meets floor -----
    res = _resources(
        community=[
            _community("c8gb", vram=8, price=0.05),
            _community("c16gb", vram=16, price=0.10),
        ],
        caps=[],
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=10, frame_step=1, total_frames=10,
        resources=res,
        heaviness=parse_analysis_heaviness(None, file_size_bytes=10 * 1024 ** 3),  # floor=48
    )
    assert len(tasks) > 0, "should fall back to no floor when no eligible meets it"
    print(f"7. VRAM floor fallback (10GB file, all eligibles below floor)         OK")

    # ----- 8. Even frame distribution with residual -----
    # 100 frames / 6 machines -> 4 of size 17, 2 of size 16 (sums to 100)
    res = _resources(
        community=[_community(f"c{i}", price=0.10 + i * 0.01) for i in range(8)],
        caps=[],
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=100, frame_step=1, total_frames=100,
        resources=res,
    )
    chunk_sizes = sorted([t.total_frames for t in tasks], reverse=True)
    total = sum(chunk_sizes)
    assert total == 100, f"distribution must sum to 100, got {total}"
    # Residual distribution: max - min should be 0 or 1
    assert max(chunk_sizes) - min(chunk_sizes) <= 1, \
        f"chunks should differ by at most 1: {chunk_sizes}"
    print(f"8. even distribution with residual: 100 frames -> chunk sizes {chunk_sizes}   OK")

    # ----- 9. Tiebreaker: same price, faster wins -----
    # 6 frames -> max_picks = 2; both same-priced targets eligible; faster
    # is sorted first and gets the first chunk.
    res = _resources(
        community=[
            _community("slow", price=0.30, speed=1.0),
            _community("fast", price=0.30, speed=1.4),
        ],
        caps=[],
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=6, frame_step=1, total_frames=6,
        resources=res,
    )
    assert len(tasks) == 2
    # Both got picked, but the FIRST chunk should go to the faster one
    # (since sort puts faster first among same-price).
    assert tasks[0].machine_id == "fast", \
        f"faster should be picked first among same-price: got {tasks[0].machine_id}"
    print(f"9. tiebreaker among same-price: faster picked first                   OK")

    # ----- 10. allocate_retry picks cheapest of remaining -----
    res = _resources(
        community=[
            _community("c1", price=0.20),
            _community("c2", price=0.30),
            _community("c3", price=0.40),
        ],
        caps=[],
    )
    req = AllocationChunkRequest(
        group_id="g1", chunk_index=0,
        frame_start=1, frame_end=5, frame_step=1, total_frames=5,
        attempt=1,
        excluded_machine_ids=("c1",),   # exclude cheapest
        excluded_serverless_capabilities=(),
    )
    retry = strategy.allocate_retry(req, res)
    assert retry is not None
    assert retry.machine_id == "c2", \
        f"retry should pick next-cheapest after exclusion: got {retry.machine_id}"
    print(f"10. retry picks next-cheapest after anti-affinity exclusion           OK")

    # ----- 11. allocate_retry returns None when all excluded -----
    req = AllocationChunkRequest(
        group_id="g1", chunk_index=0,
        frame_start=1, frame_end=5, frame_step=1, total_frames=5,
        attempt=1,
        excluded_machine_ids=("c1", "c2", "c3"),
        excluded_serverless_capabilities=(),
    )
    retry = strategy.allocate_retry(req, res)
    assert retry is None, "should return None when all eligibles excluded"
    print(f"11. retry returns None when all targets excluded                      OK")

    # ----- 12. Engine-compat validator excludes Modal for EEVEE -----
    from serverV2.allocation.allocation_strategies.validators.allocation_engine_compatibility_validator import (
        AllocationEngineCompatibilityValidator,
    )
    res = _resources(
        community=[],
        caps=[
            _modal_cap("h100", price=0.50, max_par=10),  # cheapest, but EEVEE-incompatible
            _vast_cap("RTX 4090", price=1.00, max_par=10),
        ],
    )
    eevee_strategy = EconomyAllocationStrategy(registry, validators=[AllocationEngineCompatibilityValidator()])
    tasks = eevee_strategy.allocate_initial(
        frame_start=1, frame_end=5, frame_step=1, total_frames=5,
        resources=res,
        engine="BLENDER_EEVEE",
    )
    fleets = [t.fleet for t in tasks]
    assert "modal_serverless" not in fleets, \
        "modal should be filtered out for EEVEE engine"
    assert all(f == "vast_serverless" for f in fleets)
    print(f"12. EEVEE engine filters out modal_serverless                          OK")

    # ----- 13. Fleet capacity respected (in_flight) -----
    res = _resources(
        community=[],
        caps=[_modal_cap("l4", price=0.20, max_par=15)],
        in_flight={"modal_serverless": 14},   # only 1 slot left
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=10, frame_step=1, total_frames=10,
        resources=res,
    )
    modal_count = sum(1 for t in tasks if t.fleet == "modal_serverless")
    assert modal_count == 1, f"only 1 modal slot should be offered, got {modal_count}"
    print(f"13. fleet capacity respected: 14/15 in flight -> 1 slot offered       OK")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    _smoke()
