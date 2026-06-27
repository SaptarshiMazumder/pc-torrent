"""Affinity behaviour in AllocationPlanner.plan_retry.

Semantics under test (decision (ii)):
  * a combo that started/rendered the group (``preferred_*``) wins outright
    on retry -- over community-first AND the fleet-share split;
  * EXCEPT a combo that failed THIS chunk (``excluded_*``) stays excluded,
    even if it's affine (anti-affinity is per-chunk and wins);
  * empty affinity -> the normal community-first / fleet-share selection.
"""

from __future__ import annotations

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.allocation_strategies.allocation_planner import (
    AllocationPlanner,
)
from serverV2.core.models import AvailableResources
from serverV2.tests.allocation.planner_scenarios import (
    MEDIUM_HEAVINESS,
    StubRegistry,
    _community,
    _default_startup_buffer,
    _default_vram_boost,
    _default_weights,
    _modal,
    _vast,
)


# Community + vast + modal all eligible.  Without affinity, community-first
# would win the single retry slot; affinity lets us steer it.
def _mixed_resources() -> AvailableResources:
    return AvailableResources(
        community_machines=[
            _community(id_="pc-alpha", gpu="RTX 4090", vram=24.0, speed=1.60),
        ],
        serverless_capabilities=[
            _vast(offer_id=7001, gpu="RTX 4090", vram=24.0, speed=1.70, price=0.45),
            _modal(gpu="H100", vram=80.0, speed=2.40, price=4.50),
        ],
        serverless_in_flight={"vast_serverless": 0, "modal_serverless": 0},
    )


def _plan_retry(resources, **req_kwargs):
    req = AllocationChunkRequest(
        group_id="g", chunk_index=0,
        frame_start=1, frame_end=20, frame_step=1, total_frames=20,
        attempt=1, engine="cycles", **req_kwargs,
    )
    return AllocationPlanner(registry=StubRegistry()).plan_retry(
        req, resources, weights=_default_weights(),
        startup_buffer_sec=_default_startup_buffer(),
        vram_fleet_boost=_default_vram_boost(),
        heaviness=MEDIUM_HEAVINESS,
    )


def test_no_affinity_falls_back_to_community_first():
    task = _plan_retry(_mixed_resources())
    assert task is not None
    assert task.fleet == "community", task.fleet


def test_affinity_wins_over_community_first():
    # Vast RTX 4090 rendered the group before -> it wins the retry even
    # though community would normally be taken first.
    task = _plan_retry(
        _mixed_resources(),
        preferred_serverless_capabilities=(("vast_serverless", "RTX 4090"),),
    )
    assert task is not None
    assert task.fleet == "vast_serverless", task.fleet
    assert task.gpu_type == "RTX 4090", task.gpu_type


def test_same_chunk_failure_excludes_even_an_affine_combo():
    # The vast RTX 4090 is BOTH affine (rendered elsewhere) AND failed THIS
    # chunk.  Per (ii) the exclusion wins: it is not chosen; the planner
    # falls back to the normal selection (community-first).
    task = _plan_retry(
        _mixed_resources(),
        preferred_serverless_capabilities=(("vast_serverless", "RTX 4090"),),
        excluded_serverless_capabilities=(("vast_serverless", "RTX 4090"),),
    )
    assert task is not None
    assert not (task.fleet == "vast_serverless" and task.gpu_type == "RTX 4090"), task
    assert task.fleet == "community", task.fleet


def test_affinity_prefers_modal_when_modal_is_the_proven_combo():
    # Affinity is generic: prefer whatever combo proved itself.  Here modal
    # H100 is the affine one and wins over community-first.
    task = _plan_retry(
        _mixed_resources(),
        preferred_serverless_capabilities=(("modal_serverless", "H100"),),
    )
    assert task is not None
    assert task.fleet == "modal_serverless", task.fleet
    assert task.gpu_type == "H100", task.gpu_type
