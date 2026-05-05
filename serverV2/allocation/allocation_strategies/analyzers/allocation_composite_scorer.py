"""CompositeScorer -- single-target ranking score combining speed and cost.

Pure module.  Given:

  * a target's specs (``render_speed``, ``price_per_hour``)
  * the scene's heaviness (full dict from ``parse_analysis_heaviness``)
  * an estimated chunk size (frames the target will own)
  * an ``AllocationWeights`` instance from the calling strategy

returns a single float -- higher = better.  The planner sorts the
eligible pool by this score to pick the mix.

Composite shape::

    chunk_seconds = startup(heaviness) + spf(heaviness, render_speed) * frames
    chunk_cost    = chunk_seconds / 3600 * price_per_hour
    speed_factor  = REF_SECONDS / max(chunk_seconds, 1.0)
    cost_factor   = REF_COST    / max(chunk_cost,    0.001)
    score         = w.speed * speed_factor + w.cost * cost_factor

The reference values normalise the two factors so they live in roughly
the same range.  The composite is unit-less; only relative ordering
matters across a single scoring pass.

Both ``estimate_seconds_per_frame`` and ``estimate_startup_seconds``
already account for vertex_count, shader_node_count, samples,
volumetrics, SSS, particles, geometry_nodes, etc. -- nothing in
heaviness is ignored.
"""

from __future__ import annotations

from typing import Any

from serverV2.allocation.allocation_strategies.allocation_weights import (
    AllocationWeights,
)
from serverV2.allocation.allocation_strategies.analyzers.allocation_time_analyzer import (
    estimate_seconds_per_frame,
    estimate_startup_seconds,
)


# Reference values for normalising the two factors so they live in the
# same numeric range.  Picked so a "baseline scene on baseline hardware"
# yields ~1.0 for both factors.
#
# REF_SECONDS: 5 minutes of render = 300 s.  A target that completes a
#              chunk in 300s gets speed_factor = 1.0; faster -> >1.0.
#
# REF_COST: $0.05 per chunk.  A target costing $0.05 gets cost_factor =
#           1.0; cheaper -> >1.0.
#
# These are deliberately on the cheap-and-fast side so most real
# targets score below 1.0 on both axes; the weighted sum still preserves
# correct ordering and the constants don't need empirical tuning.
REF_SECONDS = 300.0
REF_COST = 0.05


# Floors to avoid divide-by-zero / runaway scores on misconfigured inputs.
_MIN_CHUNK_SECONDS = 1.0
_MIN_CHUNK_COST = 0.001


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def score_target(
    *,
    render_speed: float,
    price_per_hour: float,
    heaviness: dict[str, Any],
    estimated_chunk_frames: int,
    weights: AllocationWeights,
) -> float:
    """Composite speed+cost score for a single target.  Higher = better."""
    seconds = chunk_seconds_for(
        render_speed=render_speed,
        heaviness=heaviness,
        estimated_chunk_frames=estimated_chunk_frames,
    )
    cost = chunk_cost_for(
        render_speed=render_speed,
        price_per_hour=price_per_hour,
        heaviness=heaviness,
        estimated_chunk_frames=estimated_chunk_frames,
    )
    speed_factor = REF_SECONDS / max(seconds, _MIN_CHUNK_SECONDS)
    cost_factor = REF_COST / max(cost, _MIN_CHUNK_COST)
    return weights.speed_weight * speed_factor + weights.cost_weight * cost_factor


def chunk_seconds_for(
    *,
    render_speed: float,
    heaviness: dict[str, Any],
    estimated_chunk_frames: int,
) -> float:
    """Estimated wall-time (seconds) for a chunk of ``estimated_chunk_frames``
    frames running on a target with this ``render_speed``.

    Splits cleanly into startup (one-time per chunk) + per-frame work.
    Used both internally by the scorer and externally to stamp
    ``estimated_seconds`` onto the resulting ``PlannedTask``.
    """
    spf = estimate_seconds_per_frame(heaviness, render_speed)
    startup = estimate_startup_seconds(heaviness)
    return startup + spf * max(0, int(estimated_chunk_frames))


def chunk_cost_for(
    *,
    render_speed: float,
    price_per_hour: float,
    heaviness: dict[str, Any],
    estimated_chunk_frames: int,
) -> float:
    """Estimated USD cost for a chunk on a target.  Hourly billing is
    Vast/Modal's model; community machines are nominally free at the
    moment (price_per_hour very low) but the formula still applies.
    """
    seconds = chunk_seconds_for(
        render_speed=render_speed,
        heaviness=heaviness,
        estimated_chunk_frames=estimated_chunk_frames,
    )
    return (seconds / 3600.0) * max(0.0, float(price_per_hour or 0.0))


# ---------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------

def _smoke() -> None:
    print("=== CompositeScorer smoke test ===\n")
    from serverV2.core.value_objects import parse_analysis_heaviness
    from serverV2.allocation.allocation_strategies.allocation_weights import (
        ECONOMY, STANDARD, PREMIUM,
    )

    base = parse_analysis_heaviness(None)

    # ----- Cheap+slow vs expensive+fast on baseline scene -----
    cheap_slow = {"render_speed": 0.5, "price_per_hour": 0.10}     # community-ish
    expensive_fast = {"render_speed": 2.5, "price_per_hour": 1.50} # H100-ish

    for label, w in [("ECONOMY", ECONOMY), ("STANDARD", STANDARD), ("PREMIUM", PREMIUM)]:
        s_cheap = score_target(
            render_speed=cheap_slow["render_speed"],
            price_per_hour=cheap_slow["price_per_hour"],
            heaviness=base, estimated_chunk_frames=10, weights=w,
        )
        s_fast = score_target(
            render_speed=expensive_fast["render_speed"],
            price_per_hour=expensive_fast["price_per_hour"],
            heaviness=base, estimated_chunk_frames=10, weights=w,
        )
        winner = "cheap" if s_cheap > s_fast else "fast"
        print(f"{label:9} cheap_slow={s_cheap:7.2f}  expensive_fast={s_fast:7.2f}  -> {winner} wins")

    # ECONOMY should pick cheap_slow.  PREMIUM should pick expensive_fast.
    s_e_cheap = score_target(render_speed=0.5, price_per_hour=0.10,
                              heaviness=base, estimated_chunk_frames=10, weights=ECONOMY)
    s_e_fast = score_target(render_speed=2.5, price_per_hour=1.50,
                             heaviness=base, estimated_chunk_frames=10, weights=ECONOMY)
    assert s_e_cheap > s_e_fast, "Economy should prefer cheap-slow"

    s_p_cheap = score_target(render_speed=0.5, price_per_hour=0.10,
                              heaviness=base, estimated_chunk_frames=10, weights=PREMIUM)
    s_p_fast = score_target(render_speed=2.5, price_per_hour=1.50,
                             heaviness=base, estimated_chunk_frames=10, weights=PREMIUM)
    assert s_p_fast > s_p_cheap, "Premium should prefer expensive-fast"

    # ----- Heavy scene amplifies the speed advantage of fast cards -----
    heavy = parse_analysis_heaviness(None)
    heavy["vertex_count_total"] = 50_000_000
    heavy["uses_volumetrics"] = True
    heavy["samples"] = 4096

    for label, w in [("ECONOMY", ECONOMY), ("STANDARD", STANDARD), ("PREMIUM", PREMIUM)]:
        s_cheap = score_target(render_speed=0.5, price_per_hour=0.10,
                                heaviness=heavy, estimated_chunk_frames=10, weights=w)
        s_fast = score_target(render_speed=2.5, price_per_hour=1.50,
                               heaviness=heavy, estimated_chunk_frames=10, weights=w)
        print(f"HEAVY {label:9} cheap_slow={s_cheap:7.2f}  expensive_fast={s_fast:7.2f}")

    # ----- chunk_seconds + chunk_cost helpers -----
    sec = chunk_seconds_for(render_speed=1.0, heaviness=base, estimated_chunk_frames=10)
    cost = chunk_cost_for(render_speed=1.0, price_per_hour=0.30,
                           heaviness=base, estimated_chunk_frames=10)
    expected_cost = sec / 3600.0 * 0.30
    assert abs(cost - expected_cost) < 0.001
    print(f"\nchunk_seconds (1.0x speed, 10 frames) -> {sec:6.1f} s")
    print(f"chunk_cost ($0.30/hr, 10 frames)      -> ${cost:.4f}")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    _smoke()
