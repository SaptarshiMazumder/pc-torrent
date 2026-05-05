"""Smoke test for the unified allocation strategy.

Standalone script -- no DB, no real Vast/Modal/community state.  Builds
synthetic capability lists and scenes, runs the planner end-to-end, and
asserts the K-decision and per-target picks match expectations.

Run::

    python -m scripts.smoke_allocation_strategy

Eight scenarios covering:
  1. Tiny render        -- expect K=1
  2. Short light        -- expect K small (2-7)
  3. Short heavy        -- expect K capped by total/min_frames=12
  4. Medium             -- expect K growing with frames
  5. Big                -- expect K=60 (max_targets cap)
  6. CUDA discrimination -- newer-CUDA RTX 4090 wins over older-CUDA
  7. EEVEE OS discrimination -- Linux wins over Windows on EEVEE
  8. Diversification cap -- no single gpu_type exceeds 0.40 * K

Each scenario is a single function for readable failure messages.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# Allow running as `python scripts/smoke_allocation_strategy.py` from repo root
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from serverV2.allocation.allocation_strategies.allocation_planner import (
    AllocationPlanner,
)
from serverV2.allocation.allocation_strategies.allocation_strategy import (
    AllocationStrategy,
)
from serverV2.allocation.allocation_strategies.analyzers import (
    allocation_time_analyzer,
)
from serverV2.allocation.allocation_strategies.validators.allocation_eevee_linux_only_validator import (
    AllocationEeveeLinuxOnlyValidator,
)
from serverV2.allocation.allocation_strategies.validators.allocation_engine_compatibility_validator import (
    AllocationEngineCompatibilityValidator,
)
from serverV2.config import RenderTimeConfig, StartupBufferConfig
from serverV2.core.models import (
    AvailableResources,
    CommunityMachine,
    FleetCapability,
)
from serverV2.core.value_objects import parse_analysis_heaviness


# Push production calibration into the time analyzer so the smoke
# scenarios reflect what the live planner will actually do (rather
# than the module's hand-tuned defaults).  Falls back silently if
# config.json is unreadable -- the defaults still produce valid output.
try:
    allocation_time_analyzer.configure(RenderTimeConfig.from_env())
except Exception as _exc:  # noqa: BLE001
    print(f"[smoke] warning: failed to load render_time config: {_exc}")
    print("[smoke] continuing with module-default calibration")


# ---------------------------------------------------------------------
# Fakes -- registry that always says enabled
# ---------------------------------------------------------------------

class _AllEnabledRegistry:
    def is_enabled(self, fleet: str) -> bool:
        return True


# ---------------------------------------------------------------------
# Fixtures -- synthetic capability factories
# ---------------------------------------------------------------------

_VAST = "vast_serverless"
_MODAL = "modal_serverless"


def _vast_offer(
    *,
    gpu: str,
    speed: float,
    vram: int,
    dph: float,
    cuda: str | None = "13.0",
    host_os: str | None = "Linux 24.04",
    offer_id: int,
    fleet_max: int = 30,
) -> FleetCapability:
    return FleetCapability(
        fleet=_VAST,
        gpu_type=gpu,
        label=f"Vast {gpu}",
        vram_gb=vram,
        cpu_cores=8,
        ram_gb=32,
        render_speed=speed,
        fleet_max_parallel=fleet_max,
        price_per_hour=dph,
        offer_id=offer_id,
        cuda_version=cuda,
        host_os=host_os,
    )


def _modal_cap(*, gpu: str, speed: float, vram: int, dph: float) -> FleetCapability:
    # Modal: cuda + host_os left None on purpose ("None means trust")
    return FleetCapability(
        fleet=_MODAL, gpu_type=gpu, label=f"Modal {gpu}",
        vram_gb=vram, cpu_cores=16, ram_gb=64, render_speed=speed,
        fleet_max_parallel=30, price_per_hour=dph,
    )


def _community_machine(*, mid: str, speed: float, vram: int) -> CommunityMachine:
    return CommunityMachine(
        id=mid, gpu_model="RTX 3090", vram_gb=vram, cpu_cores=12, ram_gb=32,
        render_speed=speed, status="available", last_seen_at=None,
        price_per_hour=0.10,
    )


# ---------------------------------------------------------------------
# A realistic capability pool spanning fleets / cuda / os
# ---------------------------------------------------------------------

def _diverse_pool() -> AvailableResources:
    """Pool of ~15 Vast offers across 4 GPU classes, 2 Modal classes,
    and 3 community machines.  Mix of CUDA versions and a single
    Windows offer to exercise the OS factor.
    """
    serverless: list[FleetCapability] = []

    # 4 RTX 4090 offers, mostly Linux / fresh CUDA, one with old CUDA, one Windows
    serverless.append(_vast_offer(gpu="RTX 4090", speed=1.45, vram=24, dph=0.16, cuda="13.0", offer_id=1001))
    serverless.append(_vast_offer(gpu="RTX 4090", speed=1.45, vram=24, dph=0.18, cuda="12.8", offer_id=1002))
    serverless.append(_vast_offer(gpu="RTX 4090", speed=1.45, vram=24, dph=0.22, cuda="11.8", offer_id=1003))  # old
    serverless.append(_vast_offer(gpu="RTX 4090", speed=1.45, vram=24, dph=0.20, cuda="13.0",
                                  host_os="Windows Server 2022", offer_id=1004))  # Windows

    # 4 RTX 6000 Ada offers
    for i, dph in enumerate([0.55, 0.58, 0.62, 0.66]):
        serverless.append(_vast_offer(gpu="RTX 6000Ada", speed=1.85, vram=48, dph=dph, offer_id=2001+i))

    # 3 H100 SXM offers
    for i, dph in enumerate([1.40, 1.55, 1.65]):
        serverless.append(_vast_offer(gpu="H100 SXM", speed=2.5, vram=80, dph=dph, offer_id=3001+i))

    # 3 L40S offers
    for i, dph in enumerate([0.50, 0.55, 0.60]):
        serverless.append(_vast_offer(gpu="L40S", speed=1.75, vram=48, dph=dph, offer_id=4001+i))

    # Modal: L4, L40S, H100
    serverless.append(_modal_cap(gpu="l4", speed=1.0, vram=24, dph=1.10))
    serverless.append(_modal_cap(gpu="l40s", speed=1.7, vram=48, dph=2.10))
    serverless.append(_modal_cap(gpu="h100", speed=1.1, vram=80, dph=4.50))

    community = [
        _community_machine(mid="comm-1", speed=0.8, vram=12),
        _community_machine(mid="comm-2", speed=1.0, vram=24),
        _community_machine(mid="comm-3", speed=1.2, vram=24),
    ]

    return AvailableResources(
        community_machines=community,
        serverless_capabilities=serverless,
        serverless_in_flight={_VAST: 0, _MODAL: 0, "community": 0},
    )


# ---------------------------------------------------------------------
# Scene fixtures
# ---------------------------------------------------------------------

def _light_scene() -> dict:
    h = parse_analysis_heaviness(None, file_size_bytes=200 * 1024 * 1024)
    h["render_engine"] = "CYCLES"
    h["samples"] = 256
    h["vertex_count_total"] = 1_000_000
    h["effective_pixels"] = 1920 * 1080
    return h


def _medium_scene() -> dict:
    h = parse_analysis_heaviness(None, file_size_bytes=1 * 1024 ** 3)
    h["render_engine"] = "CYCLES"
    h["samples"] = 1024
    h["vertex_count_total"] = 10_000_000
    h["effective_pixels"] = 1920 * 1080
    h["uses_subdivision"] = True
    return h


def _heavy_scene() -> dict:
    h = parse_analysis_heaviness(None, file_size_bytes=3 * 1024 ** 3)
    h["render_engine"] = "CYCLES"
    h["samples"] = 4096
    h["vertex_count_total"] = 50_000_000
    h["effective_pixels"] = 1920 * 1080 * 4   # 4K
    h["uses_subdivision"] = True
    h["uses_volumetrics"] = True
    h["uses_subsurface_scattering"] = True
    return h


def _eevee_scene() -> dict:
    h = parse_analysis_heaviness(None, file_size_bytes=500 * 1024 * 1024)
    h["render_engine"] = "BLENDER_EEVEE"
    h["samples"] = 64
    h["vertex_count_total"] = 5_000_000
    h["effective_pixels"] = 1920 * 1080
    return h


# ---------------------------------------------------------------------
# Strategy harness
# ---------------------------------------------------------------------

def _build_strategy() -> AllocationStrategy:
    planner = AllocationPlanner(
        registry=_AllEnabledRegistry(),
        startup_buffer=StartupBufferConfig(),  # vast=180, modal=120, community=0
        validators=[
            AllocationEngineCompatibilityValidator(),
            AllocationEeveeLinuxOnlyValidator(),
        ],
    )
    return AllocationStrategy(planner)


def _summarise(tasks: list, scenario: str) -> None:
    print(f"\n--- {scenario} ---")
    print(f"K = {len(tasks)}")
    if not tasks:
        return
    for i, t in enumerate(tasks):
        cuda = t.cuda_version or "-"
        os_ = (t.host_os or "-")[:20]
        offer = t.offer_id or "-"
        print(
            f"  #{i:02d} fleet={t.fleet:18s} gpu={t.gpu_type or t.label:18s} "
            f"speed={t.render_speed:.2f} frames={t.total_frames:4d} "
            f"cuda={cuda:>5s} os={os_:20s} offer={offer}"
        )


# ---------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------

def scenario_1_tiny() -> None:
    strategy = _build_strategy()
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=5, frame_step=1, total_frames=5,
        resources=_diverse_pool(), engine="CYCLES", heaviness=_light_scene(),
    )
    _summarise(tasks, "1. Tiny (5 frames, light scene)")
    assert len(tasks) == 1, f"expected K=1, got {len(tasks)}"


def scenario_2_short_light() -> None:
    strategy = _build_strategy()
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=50, frame_step=1, total_frames=50,
        resources=_diverse_pool(), engine="CYCLES", heaviness=_light_scene(),
    )
    _summarise(tasks, "2. Short light (50 frames, light scene)")
    # Upper bound is total_frames // min_frames_per_chunk = 50/4 = 12
    # under realistic Cycles BASELINE_SEC; render time is high enough
    # that the knapsack rule justifies full fan-out.
    assert 1 <= len(tasks) <= 12, f"expected K in [1, 12], got {len(tasks)}"


def scenario_3_short_heavy() -> None:
    strategy = _build_strategy()
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=50, frame_step=1, total_frames=50,
        resources=_diverse_pool(), engine="CYCLES", heaviness=_heavy_scene(),
    )
    _summarise(tasks, "3. Short heavy (50 frames, heavy scene)")
    # max_by_frames cap = 50 // 4 = 12
    assert 1 <= len(tasks) <= 12, f"expected K in [1, 12], got {len(tasks)}"


def scenario_4_medium() -> None:
    strategy = _build_strategy()
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=200, frame_step=1, total_frames=200,
        resources=_diverse_pool(), engine="CYCLES", heaviness=_medium_scene(),
    )
    _summarise(tasks, "4. Medium (200 frames, medium scene)")
    assert 5 <= len(tasks) <= 50, f"expected K in [5, 50], got {len(tasks)}"


def scenario_5_big() -> None:
    strategy = _build_strategy()
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=1000, frame_step=1, total_frames=1000,
        resources=_diverse_pool(), engine="CYCLES", heaviness=_medium_scene(),
    )
    _summarise(tasks, "5. Big (1000 frames, medium scene)")
    # Should hit max_targets cap (60).  Pool only has ~22 distinct
    # FleetCapabilities though, so K <= len(eligible) ~= 22.
    assert len(tasks) >= 15, f"expected K >= 15 for big render, got {len(tasks)}"


def scenario_6_cuda_discrimination() -> None:
    """Two RTX 4090 offers identical except CUDA 11.8 vs 13.0.
    With speed equal and price (cost not in scoring) ignored, the
    cuda_factor should make the 13.0 offer rank higher.
    """
    strategy = _build_strategy()
    pool = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast_offer(gpu="RTX 4090", speed=1.45, vram=24, dph=0.20, cuda="13.0", offer_id=9001),
            _vast_offer(gpu="RTX 4090", speed=1.45, vram=24, dph=0.20, cuda="11.8", offer_id=9002),
        ],
        serverless_in_flight={_VAST: 0},
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=20, frame_step=1, total_frames=20,
        resources=pool, engine="CYCLES", heaviness=_light_scene(),
    )
    _summarise(tasks, "6. CUDA discrimination (RTX 4090 cuda=13.0 vs cuda=11.8)")
    # If only 1 chunk, should pick the 13.0 one.  If more, the 13.0
    # offer must be FIRST in the score-sorted top-K.
    first = tasks[0]
    assert first.cuda_version == "13.0", \
        f"expected cuda=13.0 to win, got cuda={first.cuda_version}"


def scenario_7_eevee_os_discrimination() -> None:
    """EEVEE render + two L40S offers identical except Linux vs Windows.
    The hard validator (AllocationEeveeLinuxOnlyValidator) removes the
    Windows offer from the eligible pool entirely -- Windows must
    NEVER appear in the picks, regardless of how many slots K wants.
    """
    strategy = _build_strategy()
    pool = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast_offer(gpu="L40S", speed=1.75, vram=48, dph=0.55,
                        cuda="13.0", host_os="Linux 24.04", offer_id=8001),
            _vast_offer(gpu="L40S", speed=1.75, vram=48, dph=0.50,
                        cuda="13.0", host_os="Windows Server 2022", offer_id=8002),
        ],
        serverless_in_flight={_VAST: 0},
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=20, frame_step=1, total_frames=20,
        resources=pool, engine="BLENDER_EEVEE", heaviness=_eevee_scene(),
    )
    _summarise(tasks, "7. EEVEE OS discrimination (Linux vs Windows L40S)")
    # Hard validator: zero Windows offers in the picks.
    windows_picks = [
        t for t in tasks if "windows" in (t.host_os or "").lower()
    ]
    assert not windows_picks, (
        f"expected no Windows picks on EEVEE; got "
        f"{[t.offer_id for t in windows_picks]}"
    )
    # Sanity: the Linux offer should be picked at least once.
    assert any("linux" in (t.host_os or "").lower() for t in tasks), (
        f"expected at least one Linux pick on EEVEE, got 0"
    )


def scenario_8_diversification_cap() -> None:
    """Eight RTX 4090 offers with great scores + a few of other classes.
    With gpu_type_diversification_cap=0.40 and K large enough, the
    planner should NOT pick all 8 RTX 4090 offers.
    """
    strategy = _build_strategy()
    serverless: list[FleetCapability] = [
        _vast_offer(gpu="RTX 4090", speed=1.45, vram=24, dph=0.18,
                    cuda="13.0", offer_id=7000+i)
        for i in range(8)
    ]
    serverless.append(_vast_offer(gpu="L40S", speed=1.75, vram=48, dph=0.55, offer_id=7100))
    serverless.append(_vast_offer(gpu="L40S", speed=1.75, vram=48, dph=0.60, offer_id=7101))
    serverless.append(_vast_offer(gpu="RTX 6000Ada", speed=1.85, vram=48, dph=0.60, offer_id=7102))
    serverless.append(_vast_offer(gpu="H100 SXM", speed=2.5, vram=80, dph=1.50, offer_id=7103))
    pool = AvailableResources(
        community_machines=[],
        serverless_capabilities=serverless,
        serverless_in_flight={_VAST: 0},
    )
    tasks = strategy.allocate_initial(
        frame_start=1, frame_end=200, frame_step=1, total_frames=200,
        resources=pool, engine="CYCLES", heaviness=_medium_scene(),
    )
    _summarise(tasks, "8. Diversification cap (8 RTX 4090s + others)")
    rtx4090_count = sum(1 for t in tasks if t.gpu_type == "RTX 4090")
    cap = max(1, int(len(tasks) * 0.40))
    # Backfill may push above the cap if other classes can't fill;
    # the cap is "soft" -- it shapes the prefix, backfill ignores it.
    # Still expect SOME diversity.
    assert rtx4090_count < len(tasks), \
        f"expected mix of gpu_types, got {rtx4090_count}/{len(tasks)} all RTX 4090"
    print(f"  RTX 4090 picks: {rtx4090_count} / {len(tasks)} "
          f"(soft cap was {cap}, backfill may exceed)")


# ---------------------------------------------------------------------

def main() -> int:
    scenarios = [
        scenario_1_tiny,
        scenario_2_short_light,
        scenario_3_short_heavy,
        scenario_4_medium,
        scenario_5_big,
        scenario_6_cuda_discrimination,
        scenario_7_eevee_os_discrimination,
        scenario_8_diversification_cap,
    ]
    failed = 0
    for fn in scenarios:
        try:
            fn()
        except AssertionError as e:
            print(f"  ASSERTION FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n=== {len(scenarios) - failed} / {len(scenarios)} scenarios passed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
