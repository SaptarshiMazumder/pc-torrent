"""FastRenderAllocationStrategy — heaviness-aware, fan-out-aware allocator.

Selects a *mix* of fleet targets sized to the scene's heaviness and
distributes frames across them weighted by render_speed.  Used for the
"Standard" tier: bigger or many-frame renders where the Default's
"one chunk per eligible target" is too coarse.

Logic per ``allocate_initial`` call:

  1. Determine heaviness band from ``file_size_bytes``.
  2. Filter eligible targets: drop anything with ``vram_gb < scene_min_vram``.
     Fall back to "anything available" if nothing fits (better to try than
     refuse).
  3. Compute desired machine count: ``ceil(total_frames / target_per_machine)``,
     clamped to ``HARD_CAP``.
  4. **Value-first selection** respecting fleet caps: highest
     ``compute_power_score / sqrt(price_per_hour)`` first.  This stops the
     "H100 picked over cheaper-and-faster L40S/4090" failure mode.
  5. Distribute frames across the selected mix, weighted by
     ``compute_power_score`` (so faster cards get more frames).
  6. **Soft budget cap (Phase 7)** — if ``tier_budget_usd`` was provided
     and the picked mix's estimated cost exceeds ``tier_budget * 1.5``,
     drop the most expensive target and re-distribute.  Repeat until
     within budget or only one target remains.
"""

from __future__ import annotations

import math

from serverV2.allocation.frame_distributor import distribute_frames
from serverV2.allocation.power_scorer import compute_power_score
from serverV2.core.models import (
    AvailableResources,
    CommunityMachine,
    PlannedTask,
)
from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.allocation.analyzers.cost_analyzer import (
    MixSlot,
    estimate_cost_for_mix,
)
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest
from serverV2.orchestrator.allocation.heaviness_bands import band_for
from serverV2.orchestrator.allocation.validators.target_validator import (
    TargetValidator,
)
from serverV2.orchestrator.allocation.validators.validation_context import (
    ValidationContext,
)

# How many parallel containers a single render is willing to fan out to.
# Below this we'd be wasting frames on too-few machines; above this the
# coordination overhead and Modal/Vast spin-up tax kicks in.
HARD_CAP = 12

# Cost-softening exponent for the value score.  0.5 = sqrt (Phase 7
# default — fast machines still win when worth it; 1.0 would be strict
# speed-per-dollar; 0.0 would be price-blind, today's behaviour).
PRICE_SOFTENING_EXPONENT = 0.5

# Soft budget headroom — the mix's estimated cost may exceed
# ``tier_budget`` by up to this multiplier before we start dropping
# targets.  Generous so the soft cap rarely fires for slightly-over
# mixes.
BUDGET_CAP_MULTIPLIER = 1.5

# Floor on ``price_per_hour`` to avoid divide-by-zero on misconfigured
# targets (Phase 1 stamps $1/hr on community, but be defensive).
_MIN_PRICE_FOR_SCORE = 0.001


def _value_score(target) -> float:
    """Cost-aware ranking score: speed-per-dollar with sqrt softening.

    score = compute_power_score(target) / max(0.001, price_per_hour) ** 0.5

    sqrt softens the price term so a marginally-faster-much-more-expensive
    target doesn't entirely lose to the cheapest option — the cost penalty
    is meaningful but not dominant.

    Used for both target selection (``_select_mix``) and retry pick.
    Frame distribution within a selected mix still uses raw
    ``compute_power_score`` via the shared ``distribute_frames`` helper.
    """
    base = compute_power_score(target)
    price = max(_MIN_PRICE_FOR_SCORE, getattr(target, "price_per_hour", 0.0) or 0.0)
    return base / (price ** PRICE_SOFTENING_EXPONENT)


class FastRenderAllocationStrategy:

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
        file_size_bytes: int | None = None,
        engine: str | None = None,
        tier_budget_usd: float | None = None,
        heaviness: dict | None = None,
    ) -> list[PlannedTask]:
        context = ValidationContext(engine=engine)
        vram_floor, frames_per_machine = band_for(file_size_bytes)
        eligible = self._eligible_targets(resources, vram_floor, context)
        if not eligible:
            # Fall back: everything currently available.  Better to try than
            # refuse — a too-small VRAM card may still complete a smaller
            # chunk if the scene streams.
            eligible = self._eligible_targets(resources, 0, context)
        if not eligible:
            return []

        ideal_count = max(1, min(HARD_CAP, math.ceil(total_frames / frames_per_machine)))

        fleet_caps = self._fleet_caps(resources)
        in_flight = dict(resources.serverless_in_flight)

        selected = self._select_mix(eligible, ideal_count, in_flight, fleet_caps)
        if not selected:
            return []

        shares = distribute_frames(
            total_frames=total_frames,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            targets=selected,
            min_frames_fn=self._min_frames_for,
        )

        # Phase 7 — soft budget cap.  Drops the most expensive target and
        # re-distributes if estimated cost exceeds tier_budget * 1.5.  No-op
        # when tier_budget_usd or heaviness is None (today's call sites pass
        # neither; Phase 8 wires them from RenderLifecycle.plan).
        shares = self._apply_soft_budget_cap(
            shares=shares,
            total_frames=total_frames,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            tier_budget_usd=tier_budget_usd,
            heaviness=heaviness,
            file_size_bytes=file_size_bytes,
        )

        tasks: list[PlannedTask] = []
        for i, share in enumerate(shares):
            tasks.append(self._share_to_task(
                share, frame_step=frame_step, chunk_index=i, attempt=0,
            ))
        return tasks

    # ------------------------------------------------------------------
    # retry — single chunk, anti-affinity-respecting
    # ------------------------------------------------------------------

    def allocate_retry(
        self,
        chunk_request: ChunkRequest,
        resources: AvailableResources,
    ) -> PlannedTask | None:
        context = ValidationContext(engine=chunk_request.engine)
        vram_floor, _ = band_for(chunk_request.file_size_bytes)
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

        target = max(eligible, key=_value_score)
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
        context: ValidationContext,
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
            if in_flight.get(cap.fleet, 0) >= cap.fleet_max_parallel:
                continue
            if cap.vram_gb < vram_gb_min:
                continue
            if not self._passes_validators(cap, context):
                continue
            out.append(cap)
        return out

    def _passes_validators(self, target, context: ValidationContext) -> bool:
        return all(v.is_valid(target, context) for v in self._validators)

    @staticmethod
    def _fleet_caps(resources: AvailableResources) -> dict[str, int]:
        caps: dict[str, int] = {}
        for cap in resources.serverless_capabilities:
            caps[cap.fleet] = cap.fleet_max_parallel
        return caps

    @staticmethod
    def _select_mix(
        eligible: list,
        ideal_count: int,
        in_flight: dict[str, int],
        fleet_caps: dict[str, int],
    ) -> list:
        """Value-first selection: take highest ``_value_score`` (speed-per-
        sqrt-dollar) targets up to ``ideal_count``, respecting per-fleet
        headroom (each serverless pick consumes one slot of its fleet).
        Community machines are taken at most once each."""
        sorted_targets = sorted(eligible, key=_value_score, reverse=True)
        selected: list = []
        headroom = {f: cap - in_flight.get(f, 0) for f, cap in fleet_caps.items()}
        community_taken: set[str] = set()

        for target in sorted_targets:
            if len(selected) >= ideal_count:
                break
            if isinstance(target, CommunityMachine):
                if target.id in community_taken:
                    continue
                selected.append(target)
                community_taken.add(target.id)
                continue
            # FleetCapability: take as many as fleet headroom + remaining slots allow
            available = max(0, headroom.get(target.fleet, 0))
            remaining = ideal_count - len(selected)
            take = min(available, remaining)
            for _ in range(take):
                selected.append(target)
            headroom[target.fleet] = headroom.get(target.fleet, 0) - take

        return selected

    def _min_frames_for(self, target) -> int:
        if isinstance(target, CommunityMachine):
            return self._registry.min_frames_per_instance("community")
        return self._registry.min_frames_per_instance(target.fleet)

    def _apply_soft_budget_cap(
        self,
        *,
        shares: list,
        total_frames: int,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        tier_budget_usd: float | None,
        heaviness: dict | None,
        file_size_bytes: int | None,
    ) -> list:
        """Soft cap: while the picked mix's estimated cost exceeds
        ``tier_budget * BUDGET_CAP_MULTIPLIER``, drop the most-expensive
        target and re-distribute frames.  Stops at single target.

        No-op when ``tier_budget_usd`` or ``heaviness`` is None — the
        cost analyzer needs both to make a meaningful estimate.
        """
        if tier_budget_usd is None or heaviness is None:
            return shares
        if not shares or len(shares) <= 1:
            return shares

        threshold = tier_budget_usd * BUDGET_CAP_MULTIPLIER

        while len(shares) > 1:
            mix = [
                MixSlot(
                    render_speed=s.target.render_speed,
                    price_per_hour=getattr(s.target, "price_per_hour", 0.0) or 0.0,
                    frames_assigned=s.total_frames,
                )
                for s in shares
            ]
            estimate = estimate_cost_for_mix(
                heaviness, mix, file_size_bytes=file_size_bytes or 0,
            )
            if estimate.cost_mid_usd <= threshold:
                return shares

            # Drop the most expensive target — picked by raw price (the
            # value score factored speed in already; here we just want
            # to shed cost).
            idx = max(
                range(len(shares)),
                key=lambda i: getattr(shares[i].target, "price_per_hour", 0.0) or 0.0,
            )
            remaining = [s.target for i, s in enumerate(shares) if i != idx]
            if not remaining:
                return shares

            shares = distribute_frames(
                total_frames=total_frames,
                frame_start=frame_start,
                frame_end=frame_end,
                frame_step=frame_step,
                targets=remaining,
                min_frames_fn=self._min_frames_for,
            )

        return shares

    def _share_to_task(
        self, share, *, frame_step: int, chunk_index: int, attempt: int,
    ) -> PlannedTask:
        return self._target_to_task(
            share.target,
            frame_start=share.frame_start,
            frame_end=share.frame_end,
            frame_step=frame_step,
            total_frames=share.total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
        )

    @staticmethod
    def _target_to_task(
        target, *,
        frame_start: int, frame_end: int, frame_step: int, total_frames: int,
        chunk_index: int | None, attempt: int,
    ) -> PlannedTask:
        if isinstance(target, CommunityMachine):
            return PlannedTask(
                fleet="community",
                machine_id=target.id,
                gpu_type=None,
                label=target.gpu_model,
                vram_gb=target.vram_gb,
                render_speed=target.render_speed,
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
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
        )


# ---------------------------------------------------------------------------
# Smoke test — `python -m serverV2.orchestrator.allocation.fast_render_allocation_strategy`
# ---------------------------------------------------------------------------

def _smoke() -> None:
    """Hard-asserted checks for the value score + soft budget cap.

    No DB, no network — uses plain dataclass instances and a stub registry.
    """
    from serverV2.core.models import AvailableResources, FleetCapability

    print("=== FastRenderAllocationStrategy smoke test ===\n")

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

    def _vast(gpu, vram=24.0, speed=1.0, price=0.45, max_par=15):
        return FleetCapability(
            fleet="vast_serverless", gpu_type=gpu, label=f"Vast {gpu}",
            vram_gb=vram, cpu_cores=8, ram_gb=32,
            render_speed=speed, fleet_max_parallel=max_par, price_per_hour=price,
        )

    def _modal(gpu, vram=24.0, speed=1.0, price=1.10, max_par=15):
        return FleetCapability(
            fleet="modal_serverless", gpu_type=gpu, label=f"Modal {gpu}",
            vram_gb=vram, cpu_cores=8, ram_gb=32,
            render_speed=speed, fleet_max_parallel=max_par, price_per_hour=price,
        )

    def _resources(caps, in_flight=None):
        return AvailableResources(
            community_machines=[],
            serverless_capabilities=caps,
            serverless_in_flight=in_flight or {},
        )

    strategy = FastRenderAllocationStrategy(registry)

    # ----- 1. _value_score: 4090 ranks above L40S (the bug fix) -----
    rtx4090 = _vast("RTX 4090", speed=1.45, price=0.45)
    l40s = _modal("l40s", vram=48, speed=1.70, price=2.10)
    h100 = _modal("h100", vram=80, speed=1.10, price=4.50)
    a6000 = _vast("RTX A6000", vram=48, speed=1.30, price=0.55)

    s_4090 = _value_score(rtx4090)
    s_l40s = _value_score(l40s)
    s_h100 = _value_score(h100)
    s_a6000 = _value_score(a6000)
    print(f"   value_score(RTX 4090)  = {s_4090:.2f}  ({rtx4090.render_speed}x speed @ ${rtx4090.price_per_hour}/hr)")
    print(f"   value_score(L40S)      = {s_l40s:.2f}  ({l40s.render_speed}x speed @ ${l40s.price_per_hour}/hr)")
    print(f"   value_score(A6000)     = {s_a6000:.2f}  ({a6000.render_speed}x speed @ ${a6000.price_per_hour}/hr)")
    print(f"   value_score(H100)      = {s_h100:.2f}  ({h100.render_speed}x speed @ ${h100.price_per_hour}/hr)")
    # The exact A6000-vs-4090 order depends on compute_power_score (which
    # factors VRAM in) — A6000's 48GB beats 4090's 24GB on the geometry term,
    # so A6000 actually wins in our config.  What matters for this test:
    # both Vast cards beat both Modal cards, AND L40S beats H100.
    assert s_a6000 > s_l40s, f"A6000 should beat L40S on value: {s_a6000} vs {s_l40s}"
    assert s_4090 > s_l40s, f"RTX 4090 should beat L40S on value: {s_4090} vs {s_l40s}"
    assert s_l40s > s_h100, f"L40S should beat H100 on value: {s_l40s} vs {s_h100}"
    print(f"1. value_score ranking: A6000 > 4090 > L40S > H100 (Vast > Modal)   OK")

    # ----- 2. _select_mix picks value-best -----
    res = _resources(caps=[rtx4090, l40s, h100, a6000])
    fleet_caps = strategy._fleet_caps(res)
    selected = strategy._select_mix(
        eligible=[rtx4090, l40s, h100, a6000],
        ideal_count=2,
        in_flight={},
        fleet_caps=fleet_caps,
    )
    selected_gpus = [t.gpu_type for t in selected]
    # A6000 wins value by enough margin that both slots fill from vast
    # (fleet_max_parallel allows duplicates), so the picker doesn't reach
    # 4090.  Either way, no Modal target should be picked.
    assert "h100" not in selected_gpus, f"H100 should NOT be picked; got {selected_gpus}"
    assert "l40s" not in selected_gpus, f"L40S should NOT be picked; got {selected_gpus}"
    assert all(t.fleet == "vast_serverless" for t in selected), \
        f"all picks should be Vast (cheaper than Modal); got {selected_gpus}"
    print(f"2. _select_mix top-2 by value: {selected_gpus} (all Vast, Modal beaten on value)   OK")

    # ----- 3. allocate_retry picks value-best after exclusion -----
    # A6000 wins value, so we exclude it to verify 4090 is the next pick.
    res = _resources(caps=[rtx4090, l40s, h100, a6000])
    req = ChunkRequest(
        group_id="g1", chunk_index=0,
        frame_start=1, frame_end=10, frame_step=1, total_frames=10,
        attempt=1,
        excluded_machine_ids=(),
        excluded_serverless_capabilities=(("vast_serverless", "RTX A6000"),),
    )
    retry = strategy.allocate_retry(req, res)
    assert retry is not None
    assert retry.gpu_type == "RTX 4090", \
        f"retry should pick 4090 after A6000 excluded, got {retry.gpu_type}"
    print(f"3. retry picks next-value-best after exclusion: {retry.gpu_type}     OK")

    # ----- 4. tier_budget=None -> no cap (legacy callers unaffected) -----
    res = _resources(caps=[rtx4090, l40s, h100], in_flight={})
    tasks_no_budget = strategy.allocate_initial(
        frame_start=1, frame_end=60, frame_step=1, total_frames=60,
        resources=res,
        file_size_bytes=2 * 1024 ** 3,
    )
    assert len(tasks_no_budget) > 0
    print(f"4. tier_budget=None -> no cap: {len(tasks_no_budget)} tasks (legacy compat)      OK")

    # ----- 5. tier_budget given but heaviness=None -> still no cap -----
    tasks_budget_no_heaviness = strategy.allocate_initial(
        frame_start=1, frame_end=60, frame_step=1, total_frames=60,
        resources=res,
        file_size_bytes=2 * 1024 ** 3,
        tier_budget_usd=0.50,
        heaviness=None,
    )
    assert len(tasks_budget_no_heaviness) == len(tasks_no_budget), \
        "heaviness=None should disable the cap (defensive)"
    print(f"5. tier_budget given, heaviness=None -> cap skipped (defensive)       OK")

    # ----- 6. Soft budget cap drops most expensive target -----
    from serverV2.core.value_objects import parse_analysis_heaviness
    heaviness = parse_analysis_heaviness(None)
    heaviness["render_engine"] = "CYCLES"
    heaviness["samples"] = 1024

    res = _resources(caps=[rtx4090, l40s, h100, a6000], in_flight={})
    tasks_capped = strategy.allocate_initial(
        frame_start=1, frame_end=120, frame_step=1, total_frames=120,
        resources=res,
        file_size_bytes=500 * 1024 * 1024,
        tier_budget_usd=0.30,
        heaviness=heaviness,
    )
    gpus_capped = [t.gpu_type for t in tasks_capped]
    assert len(tasks_capped) >= 1
    assert "h100" not in gpus_capped, \
        f"H100 should be dropped under tight budget: {gpus_capped}"
    print(f"6. soft cap drops most-expensive (H100): "
          f"{len(tasks_capped)} targets remaining        OK")

    # ----- 7. Pathologically tiny budget -> cap stops at >=1 target -----
    tasks_min = strategy.allocate_initial(
        frame_start=1, frame_end=120, frame_step=1, total_frames=120,
        resources=res,
        file_size_bytes=500 * 1024 * 1024,
        tier_budget_usd=0.0001,
        heaviness=heaviness,
    )
    assert len(tasks_min) >= 1, "soft cap must leave at least 1 target"
    print(f"7. tiny budget -> cap stops at >=1 target ({len(tasks_min)})            OK")

    # ----- 8. Generous budget -> cap doesn't fire -----
    # Compare same inputs as test 6 (same file_size, same total_frames),
    # only the budget differs.  Generous budget should leave the mix intact
    # while tight budget (test 6) trimmed H100.
    tasks_generous = strategy.allocate_initial(
        frame_start=1, frame_end=120, frame_step=1, total_frames=120,
        resources=res,
        file_size_bytes=500 * 1024 * 1024,
        tier_budget_usd=10000.0,
        heaviness=heaviness,
    )
    tasks_no_budget_same_inputs = strategy.allocate_initial(
        frame_start=1, frame_end=120, frame_step=1, total_frames=120,
        resources=res,
        file_size_bytes=500 * 1024 * 1024,
    )
    assert len(tasks_generous) == len(tasks_no_budget_same_inputs), \
        f"generous budget should not trim: {len(tasks_generous)} vs {len(tasks_no_budget_same_inputs)}"
    assert len(tasks_generous) > len(tasks_capped), \
        f"generous budget should keep more targets than tight budget: {len(tasks_generous)} vs {len(tasks_capped)}"
    print(f"8. generous budget -> no trim ({len(tasks_generous)} = baseline; "
          f"tight=$0.30 trimmed to {len(tasks_capped)})        OK")

    # ----- 9. HARD_CAP unchanged -----
    assert HARD_CAP == 12, f"HARD_CAP must stay 12, got {HARD_CAP}"
    print(f"9. HARD_CAP unchanged at 12                                          OK")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    _smoke()

