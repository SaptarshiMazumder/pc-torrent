"""Fleet target-share guard for AllocationPlanner.

When both serverless fleets have abundant supply, the serverless slots are
apportioned by the configured ``fleet_target_share`` (default 70 modal /
30 vast).  When only one fleet has supply it takes all (spill).  Community
is taken first and is exempt from the split -- not exercised here.

The split is generic over N fleets: no fleet names appear in the planner
code, only in the config dict; these tests pin the default behaviour.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from serverV2.allocation.allocation_strategies.allocation_planner import (
    AllocationPlanner,
)
from serverV2.config.allocation_weights import (
    AllocationWeights,
)
from serverV2.core.models import AvailableResources
from serverV2.tests.allocation.planner_scenarios import (
    MEDIUM_HEAVINESS,
    StubRegistry,
    _default_startup_buffer,
    _default_vram_boost,
    _modal,
    _vast,
)


def _weights(max_targets: int, share: dict | None = None) -> AllocationWeights:
    w = AllocationWeights(
        speed_weight=0.70, cuda_weight=0.20, os_weight=0.10,
        max_targets=max_targets, min_frames_per_chunk=4,
        gpu_type_diversification_cap=0.40,
        vram_safety_factor=1.10, startup_amortization_ratio=0.5,
        chunk_count_curve=1.0, distribute_by="time_balanced",
    )
    return replace(w, fleet_target_share=share) if share is not None else w


def _plan(resources: AvailableResources, weights: AllocationWeights, frame_end: int = 2000):
    planner = AllocationPlanner(registry=StubRegistry())
    return planner.plan_initial(
        frame_start=1, frame_end=frame_end, frame_step=1, total_frames=frame_end,
        resources=resources, weights=weights,
        startup_buffer_sec=_default_startup_buffer(),
        vram_fleet_boost=_default_vram_boost(),
        engine="cycles", heaviness=MEDIUM_HEAVINESS,
    )


def _counts(tasks) -> Counter:
    return Counter(t.fleet for t in tasks)


def test_serverless_split_default_70_30():
    # Both fleets abundant, no community.  Modal carries 3 gpu_types so the
    # per-gpu-type cap (0.40) never binds on its 70% share.  K=10 -> 7/3.
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _modal(gpu="H100", vram=80.0, speed=2.40, price=4.50),
            _modal(gpu="L40S", vram=48.0, speed=1.90, price=1.80),
            _modal(gpu="L4", vram=24.0, speed=1.10, price=0.80),
            _vast(offer_id=9001, gpu="RTX 4090", vram=24.0, speed=1.72, price=0.45),
            _vast(offer_id=9002, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.46),
            _vast(offer_id=9003, gpu="RTX 4090", vram=24.0, speed=1.68, price=0.47),
            _vast(offer_id=9004, gpu="RTX 4090", vram=24.0, speed=1.66, price=0.48),
        ],
        serverless_in_flight={"modal_serverless": 0, "vast_serverless": 0},
    )
    tasks = _plan(res, _weights(max_targets=10))
    c = _counts(tasks)
    assert len(tasks) == 10, c
    assert c["modal_serverless"] == 7, c
    assert c["vast_serverless"] == 3, c


def test_custom_share_60_40():
    # The split is config-driven: a 60/40 share yields 6/4 at K=10.
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _modal(gpu="H100", vram=80.0, speed=2.40, price=4.50),
            _modal(gpu="L40S", vram=48.0, speed=1.90, price=1.80),
            _modal(gpu="L4", vram=24.0, speed=1.10, price=0.80),
            _vast(offer_id=9101, gpu="RTX 4090", vram=24.0, speed=1.72, price=0.45),
            _vast(offer_id=9102, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.46),
            _vast(offer_id=9103, gpu="RTX 4090", vram=24.0, speed=1.68, price=0.47),
            _vast(offer_id=9104, gpu="RTX 4090", vram=24.0, speed=1.66, price=0.48),
            _vast(offer_id=9105, gpu="RTX 4090", vram=24.0, speed=1.64, price=0.49),
        ],
        serverless_in_flight={"modal_serverless": 0, "vast_serverless": 0},
    )
    share = {"modal_serverless": 0.60, "vast_serverless": 0.40}
    tasks = _plan(res, _weights(max_targets=10, share=share))
    c = _counts(tasks)
    assert len(tasks) == 10, c
    assert c["modal_serverless"] == 6, c
    assert c["vast_serverless"] == 4, c


def test_only_one_fleet_takes_all():
    # Vast only -> spill: vast gets every serverless slot, modal's share is
    # not wasted on an absent fleet.
    res = AvailableResources(
        community_machines=[],
        serverless_capabilities=[
            _vast(offer_id=8001, gpu="RTX 4090", vram=24.0, speed=1.72, price=0.45),
            _vast(offer_id=8002, gpu="A100", vram=40.0, speed=2.05, price=1.30),
            _vast(offer_id=8003, gpu="RTX 3090", vram=24.0, speed=1.20, price=0.30),
        ],
        serverless_in_flight={"vast_serverless": 0},
    )
    tasks = _plan(res, _weights(max_targets=6))
    c = _counts(tasks)
    assert tasks, "expected at least one task"
    assert c.get("modal_serverless", 0) == 0, c
    assert c["vast_serverless"] == len(tasks), c
