"""Planner scenario fixtures for the golden-output regression suite.

Each builder returns the kwargs dict used to invoke
``AllocationPlanner.plan_initial`` or ``plan_retry``.  The builders are
pure -- no globals mutated, no env reads -- so the scenarios are stable
across machines.

Heaviness shape mirrors what ``SceneResolver.resolve(...)`` emits in
production: a flat dict with the fields the time / vram estimators read.
We pick concrete numbers per scenario so target scores spread out and
no tie-break dependency on Python sort stability sneaks in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.config.allocation_weights import (
    AllocationWeights,
)
from serverV2.config import StartupBufferConfig, VramFleetBoostConfig
from serverV2.core.models import (
    AvailableResources,
    CommunityMachine,
    FleetCapability,
)


# ---------------------------------------------------------------------------
# Stub registry: planner only calls is_enabled(fleet).  Tests don't need
# real fleet wiring; a constant-true (or per-fleet) stub keeps the
# scenarios self-contained and free of env / config dependencies.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StubRegistry:
    enabled: tuple[str, ...] = ("community", "vast_serverless", "modal_serverless")

    def is_enabled(self, fleet: str) -> bool:
        return fleet in self.enabled


# ---------------------------------------------------------------------------
# Heaviness profiles -- mirror the SceneResolver output shape.
# ---------------------------------------------------------------------------
LIGHT_HEAVINESS: dict[str, Any] = {
    "render_engine": "cycles",
    "estimated_seconds_per_frame_per_speed": 10.0,
    "scene_scaling_factor": 1.0,
    "required_vram_gb": 4.0,
    "startup_seconds": 60.0,
}

MEDIUM_HEAVINESS: dict[str, Any] = {
    "render_engine": "cycles",
    "estimated_seconds_per_frame_per_speed": 45.0,
    "scene_scaling_factor": 1.0,
    "required_vram_gb": 10.0,
    "startup_seconds": 90.0,
}

HEAVY_HEAVINESS: dict[str, Any] = {
    "render_engine": "cycles",
    "estimated_seconds_per_frame_per_speed": 180.0,
    "scene_scaling_factor": 1.0,
    "required_vram_gb": 22.0,
    "startup_seconds": 120.0,
}

EEVEE_HEAVINESS: dict[str, Any] = {
    "render_engine": "eevee",
    "estimated_seconds_per_frame_per_speed": 5.0,
    "scene_scaling_factor": 1.0,
    "required_vram_gb": 6.0,
    "startup_seconds": 45.0,
}


# ---------------------------------------------------------------------------
# Target factories -- distinct render_speed values guarantee distinct
# composite scores, so the sort is unambiguous and refactor-stable.
# ---------------------------------------------------------------------------
def _community(
    *, id_: str, gpu: str, vram: float, speed: float, price: float = 1.0,
) -> CommunityMachine:
    return CommunityMachine(
        id=id_,
        gpu_model=gpu,
        vram_gb=vram,
        cpu_cores=16,
        ram_gb=32.0,
        render_speed=speed,
        status="available",
        last_seen_at="2026-05-01T00:00:00+00:00",
        price_per_hour=price,
        available_seconds=None,
    )


def _vast(
    *, offer_id: int, gpu: str, vram: float, speed: float, price: float,
    cuda: str = "12.4", host_os: str = "Linux 22.04",
    available_seconds: float | None = None,
) -> FleetCapability:
    return FleetCapability(
        fleet="vast_serverless",
        gpu_type=gpu,
        label=f"Vast {gpu}",
        vram_gb=vram,
        cpu_cores=8,
        ram_gb=24.0,
        render_speed=speed,
        fleet_max_parallel=50,
        price_per_hour=price,
        offer_id=offer_id,
        cuda_version=cuda,
        host_os=host_os,
        available_seconds=available_seconds,
    )


def _modal(
    *, gpu: str, vram: float, speed: float, price: float,
    available_seconds: float | None = None,
) -> FleetCapability:
    return FleetCapability(
        fleet="modal_serverless",
        gpu_type=gpu,
        label=f"Modal {gpu}",
        vram_gb=vram,
        cpu_cores=8,
        ram_gb=32.0,
        render_speed=speed,
        fleet_max_parallel=20,
        price_per_hour=price,
        offer_id=None,
        cuda_version=None,
        host_os=None,
        available_seconds=available_seconds,
    )


# ---------------------------------------------------------------------------
# Plan-call defaults -- production-ish weights, locked so scenarios don't
# implicitly depend on AllocationWeights' field defaults shifting.
# ---------------------------------------------------------------------------
def _default_weights() -> AllocationWeights:
    return AllocationWeights(
        speed_weight=0.70,
        cuda_weight=0.20,
        os_weight=0.10,
        max_targets=8,
        min_frames_per_chunk=4,
        gpu_type_diversification_cap=0.40,
        vram_safety_factor=1.10,
        startup_amortization_ratio=0.5,
        chunk_count_curve=1.0,
        distribute_by="time_balanced",
    )


def _default_startup_buffer() -> StartupBufferConfig:
    return StartupBufferConfig(vast=180.0, modal=120.0, community=0.0)


def _default_vram_boost() -> VramFleetBoostConfig:
    return VramFleetBoostConfig(vast=1.0, modal=1.0, community=1.0)


def _wrap_initial(
    *,
    frame_start: int,
    frame_end: int,
    frame_step: int = 1,
    resources: AvailableResources,
    heaviness: dict[str, Any],
    engine: str = "cycles",
    weights: AllocationWeights | None = None,
) -> dict[str, Any]:
    """Bundle plan_initial kwargs with sensible defaults."""
    total = ((frame_end - frame_start) // frame_step) + 1
    return {
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frame_step": frame_step,
        "total_frames": total,
        "resources": resources,
        "weights": weights or _default_weights(),
        "startup_buffer_sec": _default_startup_buffer(),
        "vram_fleet_boost": _default_vram_boost(),
        "engine": engine,
        "heaviness": heaviness,
    }


def _wrap_retry(
    *,
    chunk_request: AllocationChunkRequest,
    resources: AvailableResources,
    heaviness: dict[str, Any],
    weights: AllocationWeights | None = None,
) -> dict[str, Any]:
    """Bundle plan_retry kwargs."""
    return {
        "chunk_request": chunk_request,
        "resources": resources,
        "weights": weights or _default_weights(),
        "startup_buffer_sec": _default_startup_buffer(),
        "vram_fleet_boost": _default_vram_boost(),
        "heaviness": heaviness,
    }


# ---------------------------------------------------------------------------
# plan_initial scenarios
# ---------------------------------------------------------------------------
def community_only_small() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[_community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6)],
        serverless_capabilities=[],
        serverless_in_flight={},
    )
    return _wrap_initial(frame_start=1, frame_end=20, resources=res, heaviness=LIGHT_HEAVINESS)


def community_only_large() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
            _community(id_="pc-bravo", gpu="RTX 3090", vram=24.0, speed=1.2),
            _community(id_="pc-charlie", gpu="RTX 3080", vram=10.0, speed=1.0),
        ],
        serverless_capabilities=[],
        serverless_in_flight={},
    )
    return _wrap_initial(
        frame_start=1, frame_end=5000, resources=res,
        heaviness=EEVEE_HEAVINESS, engine="eevee",
    )


def modal_only_single_gpu_type() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _modal(gpu="H100", vram=80.0, speed=2.4, price=4.50),
        ],
        serverless_in_flight={"modal_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=200, resources=res, heaviness=MEDIUM_HEAVINESS)


def vast_only_one_offer() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast(offer_id=1001, gpu="RTX 4090", vram=24.0, speed=1.7, price=0.45),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=100, resources=res, heaviness=MEDIUM_HEAVINESS)


def vast_only_many_offers_same_gpu() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast(offer_id=2001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45),
            _vast(offer_id=2002, gpu="RTX 4090", vram=24.0, speed=1.68, price=0.46),
            _vast(offer_id=2003, gpu="RTX 4090", vram=24.0, speed=1.66, price=0.47),
            _vast(offer_id=2004, gpu="RTX 4090", vram=24.0, speed=1.64, price=0.48),
            _vast(offer_id=2005, gpu="RTX 4090", vram=24.0, speed=1.62, price=0.49),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=500, resources=res, heaviness=MEDIUM_HEAVINESS)


def vast_only_many_gpu_types() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast(offer_id=3001, gpu="H100", vram=80.0, speed=2.50, price=2.20),
            _vast(offer_id=3002, gpu="A100", vram=40.0, speed=2.00, price=1.30),
            _vast(offer_id=3003, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45),
            _vast(offer_id=3004, gpu="RTX 3090", vram=24.0, speed=1.20, price=0.30),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=800, resources=res, heaviness=MEDIUM_HEAVINESS)


def mixed_three_fleets_balanced() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
            _community(id_="pc-bravo", gpu="RTX 3080", vram=10.0, speed=1.05),
        ],
        serverless_capabilities=[
            _modal(gpu="H100", vram=80.0, speed=2.40, price=4.50),
            _vast(offer_id=4001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45),
            _vast(offer_id=4002, gpu="A100", vram=40.0, speed=2.05, price=1.30),
        ],
        serverless_in_flight={"modal_serverless": 0, "vast_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=1000, resources=res, heaviness=MEDIUM_HEAVINESS)


def mixed_community_dominates() -> dict[str, Any]:
    # Community PCs given absurdly high speed so they sweep selection.
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=5.0),
            _community(id_="pc-bravo", gpu="RTX 4090", vram=24.0, speed=4.8),
            _community(id_="pc-charlie", gpu="RTX 4090", vram=24.0, speed=4.6),
        ],
        serverless_capabilities=[
            _modal(gpu="H100", vram=80.0, speed=2.4, price=4.50),
            _vast(offer_id=5001, gpu="A100", vram=40.0, speed=2.0, price=1.30),
        ],
        serverless_in_flight={"modal_serverless": 0, "vast_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=400, resources=res, heaviness=MEDIUM_HEAVINESS)


def vram_filter_normal() -> dict[str, Any]:
    # Heavy heaviness (22 GB required); some targets pass, some fail.
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
            _community(id_="pc-bravo", gpu="RTX 3060", vram=12.0, speed=0.8),
        ],
        serverless_capabilities=[
            _modal(gpu="H100", vram=80.0, speed=2.4, price=4.50),
            _vast(offer_id=6001, gpu="RTX 3070", vram=8.0, speed=0.9, price=0.20),
        ],
        serverless_in_flight={"modal_serverless": 0, "vast_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=300, resources=res, heaviness=HEAVY_HEAVINESS)


def vram_floor_fallback() -> dict[str, Any]:
    # Every target fails VRAM -- planner falls back to the no-floor path.
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 3060", vram=12.0, speed=0.8),
        ],
        serverless_capabilities=[
            _vast(offer_id=7001, gpu="RTX 3070", vram=8.0, speed=0.9, price=0.20),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=100, resources=res, heaviness=HEAVY_HEAVINESS)


def eevee_engine() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
        ],
        serverless_capabilities=[
            _vast(offer_id=8001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    return _wrap_initial(
        frame_start=1, frame_end=240, resources=res,
        heaviness=EEVEE_HEAVINESS, engine="eevee",
    )


def tiny_render_forces_k1() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
            _community(id_="pc-bravo", gpu="RTX 3090", vram=24.0, speed=1.2),
        ],
        serverless_capabilities=[],
        serverless_in_flight={},
    )
    return _wrap_initial(frame_start=1, frame_end=3, resources=res, heaviness=LIGHT_HEAVINESS)


def huge_render_hits_max_targets() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[
            _community(id_=f"pc-{i:02d}", gpu="RTX 4090", vram=24.0, speed=1.6 - i * 0.01)
            for i in range(12)
        ],
        serverless_capabilities=[],
        serverless_in_flight={},
    )
    # Cap max_targets tight (=4) so the rule binds before K_amortized would.
    weights = AllocationWeights(
        speed_weight=0.70, cuda_weight=0.20, os_weight=0.10,
        max_targets=4, min_frames_per_chunk=4,
        gpu_type_diversification_cap=0.40,
        vram_safety_factor=1.10, startup_amortization_ratio=0.5,
        chunk_count_curve=1.0, distribute_by="time_balanced",
    )
    return _wrap_initial(
        frame_start=1, frame_end=50000, resources=res,
        heaviness=MEDIUM_HEAVINESS, weights=weights,
    )


def available_seconds_none() -> dict[str, Any]:
    # Phase 1 baseline -- all targets carry available_seconds=None.
    # Planner doesn't read it yet, so output must be identical to a
    # scenario without the field at all.
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
        ],
        serverless_capabilities=[
            _vast(offer_id=9001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45,
                  available_seconds=None),
            _modal(gpu="A100", vram=40.0, speed=2.0, price=1.50,
                   available_seconds=None),
        ],
        serverless_in_flight={"vast_serverless": 0, "modal_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=400, resources=res, heaviness=MEDIUM_HEAVINESS)


def available_seconds_mixed() -> dict[str, Any]:
    # Phase 1 baseline -- some targets carry available_seconds, some
    # don't.  Planner still ignores the field.  After Phase 5, this
    # scenario's golden will need to be regenerated -- this is the
    # canary that catches the behaviour change.
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
        ],
        serverless_capabilities=[
            _vast(offer_id=9501, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45,
                  available_seconds=7200.0),
            _modal(gpu="A100", vram=40.0, speed=2.0, price=1.50,
                   available_seconds=14400.0),
        ],
        serverless_in_flight={"vast_serverless": 0, "modal_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=400, resources=res, heaviness=MEDIUM_HEAVINESS)


# ---------------------------------------------------------------------------
# Phase 5 scenarios -- exercise the time filter, headroom scoring, and
# distribution clamp.  Numbers chosen so each new path actually triggers.
# ---------------------------------------------------------------------------

def time_filter_drops_short_window() -> dict[str, Any]:
    # Two Vast offers of the same gpu_type.  Offer A has
    # available_seconds=30 which is below the vast startup floor
    # (heaviness startup 60s + vast buffer 180s = 240s base, *1.5 safety
    # = 360s).  Filter must drop A.  Offer B (None) carries every chunk.
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast(offer_id=21001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45,
                  available_seconds=30.0),
            _vast(offer_id=21002, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45,
                  available_seconds=None),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    return _wrap_initial(frame_start=1, frame_end=50, resources=res, heaviness=LIGHT_HEAVINESS)


def time_headroom_score_breaks_tie() -> dict[str, Any]:
    # Two Vast offers, identical specs but different commitment windows.
    # Both pass the filter (each > 360s vast floor).  Offer A's window
    # (450s) is below the headroom saturation threshold (1.5 * chunk
    # seconds), so its score is penalised; offer B's huge window (30000s)
    # saturates the factor at 1.0 and outranks A.
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast(offer_id=22001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45,
                  available_seconds=450.0),
            _vast(offer_id=22002, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45,
                  available_seconds=30000.0),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    # Tight max_targets so only the best-scoring offer is picked.
    weights = AllocationWeights(
        speed_weight=0.70, cuda_weight=0.20, os_weight=0.10,
        max_targets=1, min_frames_per_chunk=4,
        gpu_type_diversification_cap=0.40,
        vram_safety_factor=1.10, startup_amortization_ratio=0.5,
        chunk_count_curve=1.0, distribute_by="time_balanced",
        time_safety_factor=1.5, time_headroom_falloff=0.5,
    )
    return _wrap_initial(
        frame_start=1, frame_end=20, resources=res,
        heaviness=LIGHT_HEAVINESS, weights=weights,
    )


def time_clamp_caps_share() -> dict[str, Any]:
    # Two community machines, same speed.  Both passes filter.  The
    # time-balanced split would naturally allocate ~half the frames to
    # each (~50/100), but A's window only fits ~14 frames after startup
    # ((200 - 60) / 10s_spf).  Clamp pulls A down to 14; overflow spills
    # onto B (unbounded) which renders ~86.
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-bounded", gpu="RTX 3090", vram=24.0, speed=1.0,
                       price=1.0),
            _community(id_="pc-unbounded", gpu="RTX 3090", vram=24.0, speed=1.0,
                       price=1.0),
        ],
        serverless_capabilities=[],
        serverless_in_flight={},
    )
    # Inject a finite window on the first community machine.  ``_community``
    # always stamps None, so we rebuild it with the value.
    bounded = CommunityMachine(
        id="pc-bounded", gpu_model="RTX 3090", vram_gb=24.0, cpu_cores=16,
        ram_gb=32.0, render_speed=1.0, status="available",
        last_seen_at="2026-05-01T00:00:00+00:00", price_per_hour=1.0,
        available_seconds=200.0,
    )
    res = AvailableResources(
        community_machines=[bounded, res.community_machines[1]],
        serverless_capabilities=[],
        serverless_in_flight={},
    )
    # Force at least 2 chunks so the distributor allocates to both.
    weights = AllocationWeights(
        speed_weight=0.70, cuda_weight=0.20, os_weight=0.10,
        max_targets=2, min_frames_per_chunk=4,
        gpu_type_diversification_cap=0.40,
        vram_safety_factor=1.10, startup_amortization_ratio=0.5,
        chunk_count_curve=1.0, distribute_by="time_balanced",
        time_safety_factor=1.5, time_headroom_falloff=0.5,
    )
    return _wrap_initial(
        frame_start=1, frame_end=100, resources=res,
        heaviness=LIGHT_HEAVINESS, weights=weights,
    )


# ---------------------------------------------------------------------------
# plan_retry scenarios
# ---------------------------------------------------------------------------
def retry_single_eligible() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
        ],
        serverless_capabilities=[],
        serverless_in_flight={},
    )
    req = AllocationChunkRequest(
        group_id="grp-001", chunk_index=0,
        frame_start=1, frame_end=20, frame_step=1, total_frames=20,
        attempt=1, engine="cycles",
    )
    return _wrap_retry(chunk_request=req, resources=res, heaviness=MEDIUM_HEAVINESS)


def retry_excluded_machine_id() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
            _community(id_="pc-bravo", gpu="RTX 3090", vram=24.0, speed=1.2),
        ],
        serverless_capabilities=[
            _vast(offer_id=11001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    req = AllocationChunkRequest(
        group_id="grp-002", chunk_index=2,
        frame_start=41, frame_end=60, frame_step=1, total_frames=20,
        attempt=1, engine="cycles",
        excluded_machine_ids=("pc-alpha",),
    )
    return _wrap_retry(chunk_request=req, resources=res, heaviness=MEDIUM_HEAVINESS)


def retry_excluded_serverless_cap() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast(offer_id=12001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45),
            _modal(gpu="A100", vram=40.0, speed=2.0, price=1.50),
        ],
        serverless_in_flight={"vast_serverless": 0, "modal_serverless": 0},
    )
    req = AllocationChunkRequest(
        group_id="grp-003", chunk_index=1,
        frame_start=21, frame_end=40, frame_step=1, total_frames=20,
        attempt=1, engine="cycles",
        excluded_serverless_capabilities=(("vast_serverless", "RTX 4090"),),
    )
    return _wrap_retry(chunk_request=req, resources=res, heaviness=MEDIUM_HEAVINESS)


def retry_all_excluded() -> dict[str, Any]:
    res = AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.6),
        ],
        serverless_capabilities=[
            _vast(offer_id=13001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    req = AllocationChunkRequest(
        group_id="grp-004", chunk_index=0,
        frame_start=1, frame_end=20, frame_step=1, total_frames=20,
        attempt=2, engine="cycles",
        excluded_machine_ids=("pc-alpha",),
        excluded_serverless_capabilities=(("vast_serverless", "RTX 4090"),),
    )
    return _wrap_retry(chunk_request=req, resources=res, heaviness=MEDIUM_HEAVINESS)


# ---------------------------------------------------------------------------
# Registry of scenarios for parametrization.  Sorted name keeps the test
# IDs alphabetical and the golden directory neatly aligned.
# ---------------------------------------------------------------------------
INITIAL_SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "available_seconds_mixed": available_seconds_mixed,
    "available_seconds_none": available_seconds_none,
    "community_only_large": community_only_large,
    "community_only_small": community_only_small,
    "eevee_engine": eevee_engine,
    "huge_render_hits_max_targets": huge_render_hits_max_targets,
    "mixed_community_dominates": mixed_community_dominates,
    "mixed_three_fleets_balanced": mixed_three_fleets_balanced,
    "modal_only_single_gpu_type": modal_only_single_gpu_type,
    "time_clamp_caps_share": time_clamp_caps_share,
    "time_filter_drops_short_window": time_filter_drops_short_window,
    "time_headroom_score_breaks_tie": time_headroom_score_breaks_tie,
    "tiny_render_forces_k1": tiny_render_forces_k1,
    "vast_only_many_gpu_types": vast_only_many_gpu_types,
    "vast_only_many_offers_same_gpu": vast_only_many_offers_same_gpu,
    "vast_only_one_offer": vast_only_one_offer,
    "vram_filter_normal": vram_filter_normal,
    "vram_floor_fallback": vram_floor_fallback,
}

RETRY_SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "retry_all_excluded": retry_all_excluded,
    "retry_excluded_machine_id": retry_excluded_machine_id,
    "retry_excluded_serverless_cap": retry_excluded_serverless_cap,
    "retry_single_eligible": retry_single_eligible,
}
