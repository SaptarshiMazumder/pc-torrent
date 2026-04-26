"""CostAnalyzer — estimate (wall-time, cost-range) for a candidate mix.

Pure module.  Composes ``time_analyzer`` (per-frame seconds + per-chunk
startup) with target prices to produce a ``CostEstimate``.  Used by
the cost-aware allocators (Phases 6-7) to rank candidate mixes, and
by the cost-preview API (Phase 9) to show the user a price band on
the submit page.

Cost model::

    For each slot in the mix:
        spf      = time_analyzer.estimate_seconds_per_frame(heaviness, slot.render_speed)
        startup  = time_analyzer.estimate_startup_seconds(heaviness, file_size_bytes)
        seconds  = startup + spf * slot.frames_assigned
        cost     = seconds / 3600 * slot.price_per_hour

    wall_time = max(seconds across slots)        — chunks run in parallel
    cost_mid  = sum(cost across slots)            — every chunk's machine bills

    cost_low  = cost_mid * (1 - confidence_band)
    cost_high = cost_mid * (1 + confidence_band)

Smoke test: ``python -m serverV2.orchestrator.allocation.analyzers.cost_analyzer``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from serverV2.core.value_objects import parse_analysis_heaviness
from serverV2.orchestrator.allocation.analyzers.time_analyzer import (
    BASELINE_SEC,
    BASELINE_STARTUP_SEC,
    estimate_seconds_per_frame,
    estimate_startup_seconds,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Confidence band applied symmetrically around the point estimate.  Phase 5
# telemetry will let us derive this empirically (variance of actual /
# predicted ratios).  +/-25% is a starting guess matching the rough accuracy
# of the time analyzer's heuristic.
DEFAULT_CONFIDENCE_BAND = 0.25

# Clamp the band so callers passing pathological values can't produce
# nonsense cost ranges (e.g. negative cost_low or zero-width band).
_MIN_CONFIDENCE_BAND = 0.0
_MAX_CONFIDENCE_BAND = 0.95


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MixSlot:
    """One target's share of a candidate mix.

    Pure data — no fleet identity, no machine_id.  Just the two attributes
    the analyzer reads from each target plus the share of frames it owns.
    Both ``FleetCapability`` and ``CommunityMachine`` carry these fields
    after Phase 1, so building a slot is one line at the call site::

        slot = MixSlot(target.render_speed, target.price_per_hour, frames)
    """
    render_speed: float
    price_per_hour: float
    frames_assigned: int


@dataclass(frozen=True)
class CostEstimate:
    """Output of ``estimate_cost_for_mix``."""
    wall_time_seconds: float    # max over slots — parallel wall clock
    cost_mid_usd: float         # point estimate — sum across slots
    cost_low_usd: float         # cost_mid * (1 - confidence_band)
    cost_high_usd: float        # cost_mid * (1 + confidence_band)

    @property
    def cost_range(self) -> tuple[float, float]:
        return (self.cost_low_usd, self.cost_high_usd)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def estimate_cost_for_mix(
    heaviness: dict[str, Any],
    mix: list[MixSlot],
    *,
    file_size_bytes: int = 0,
    confidence_band: float = DEFAULT_CONFIDENCE_BAND,
) -> CostEstimate:
    """Cost + wall-time estimate for a candidate mix.

    ``heaviness`` is the already-defaulted dict from ``parse_analysis_heaviness``.

    ``mix`` is one ``MixSlot`` per target getting some share of frames.
    Empty mix → all-zero estimate (no division-by-zero, no NaNs).

    ``file_size_bytes`` feeds the startup-overhead estimator (download
    time scales with file size).  Default 0 = no download cost — useful
    for callers that don't have file size information yet.

    Each ``MixSlot`` is treated as one chunk.  Startup overhead is
    paid once per slot.  Wall time is the slowest slot's total seconds
    (chunks run in parallel); cost is the sum across slots (every
    machine bills its own seconds).
    """
    if not mix:
        return CostEstimate(0.0, 0.0, 0.0, 0.0)

    band = max(_MIN_CONFIDENCE_BAND, min(_MAX_CONFIDENCE_BAND, confidence_band))
    startup = estimate_startup_seconds(heaviness, file_size_bytes)

    wall_time = 0.0
    cost_mid = 0.0
    for slot in mix:
        if slot.frames_assigned <= 0:
            continue
        spf = estimate_seconds_per_frame(heaviness, slot.render_speed)
        seconds = startup + spf * slot.frames_assigned
        wall_time = max(wall_time, seconds)
        cost_mid += seconds / 3600.0 * max(0.0, slot.price_per_hour)

    return CostEstimate(
        wall_time_seconds=wall_time,
        cost_mid_usd=cost_mid,
        cost_low_usd=cost_mid * (1.0 - band),
        cost_high_usd=cost_mid * (1.0 + band),
    )


def estimate_cost_from_snapshot(
    analysis_snapshot: dict[str, Any] | None,
    mix: list[MixSlot],
    *,
    file_size_bytes: int = 0,
    confidence_band: float = DEFAULT_CONFIDENCE_BAND,
) -> CostEstimate:
    """Convenience wrapper — pulls the heaviness sub-dict from the raw
    analysis snapshot, then delegates to ``estimate_cost_for_mix``.

    Use this entry point when you have the raw snapshot from the DB or
    API; use ``estimate_cost_for_mix`` when you've already parsed the
    heaviness once and are calling for many candidate mixes.
    """
    return estimate_cost_for_mix(
        parse_analysis_heaviness(analysis_snapshot),
        mix,
        file_size_bytes=file_size_bytes,
        confidence_band=confidence_band,
    )


# ---------------------------------------------------------------------------
# Smoke test — ``python -m serverV2.orchestrator.allocation.analyzers.cost_analyzer``
# ---------------------------------------------------------------------------

def _smoke() -> None:
    print("=== CostAnalyzer smoke test ===\n")

    baseline = parse_analysis_heaviness(None)
    baseline["render_engine"] = "CYCLES"

    # ------- Empty mix → all zeros -------
    empty = estimate_cost_for_mix(baseline, [])
    assert empty.wall_time_seconds == 0.0
    assert empty.cost_mid_usd == 0.0
    assert empty.cost_range == (0.0, 0.0)
    print("empty mix                                 ->  all zeros            OK")

    # ------- Single slot, baseline scene, 60 frames @ render_speed=1.0, $1.00/hr -------
    # baseline spf = 30s, startup = 90s (no file, no heaviness)
    # seconds = 90 + 30 * 60 = 1890s = 31.5 min
    # cost_mid = 1890/3600 * 1.0 = $0.525
    single_mix = [MixSlot(render_speed=1.0, price_per_hour=1.0, frames_assigned=60)]
    single = estimate_cost_for_mix(baseline, single_mix)
    expected_seconds = BASELINE_STARTUP_SEC + BASELINE_SEC * 60
    assert abs(single.wall_time_seconds - expected_seconds) < 1.0
    expected_cost = expected_seconds / 3600.0 * 1.0
    assert abs(single.cost_mid_usd - expected_cost) < 0.01
    assert abs(single.cost_low_usd - expected_cost * 0.75) < 0.01
    assert abs(single.cost_high_usd - expected_cost * 1.25) < 0.01
    print(f"single slot (60 frames, $1/hr)            ->  {single.wall_time_seconds:.0f}s, "
          f"${single.cost_low_usd:.2f}-${single.cost_high_usd:.2f}    OK")

    # ------- Two slots, parallel, identical → wall_time same, cost doubled -------
    pair_same = [
        MixSlot(1.0, 1.0, 30),
        MixSlot(1.0, 1.0, 30),
    ]
    pair = estimate_cost_for_mix(baseline, pair_same)
    expected_seconds = BASELINE_STARTUP_SEC + BASELINE_SEC * 30   # each slot has 30 frames
    assert abs(pair.wall_time_seconds - expected_seconds) < 1.0
    # Cost: 2 * (seconds/3600 * $1) = 2 * single-slot cost
    assert abs(pair.cost_mid_usd - 2 * (expected_seconds / 3600.0)) < 0.01
    print(f"2 slots, parallel, equal split            ->  {pair.wall_time_seconds:.0f}s "
          f"(=single-slot wall), cost 2x              OK")

    # ------- Two slots, different speeds, equal frames → wall = slowest -------
    pair_speed = [
        MixSlot(2.0, 1.0, 30),   # fast — 30 frames at 0.5x time
        MixSlot(1.0, 1.0, 30),   # slow — 30 frames at 1.0x time
    ]
    speed_est = estimate_cost_for_mix(baseline, pair_speed)
    fast_seconds = BASELINE_STARTUP_SEC + BASELINE_SEC * 30 / 2.0
    slow_seconds = BASELINE_STARTUP_SEC + BASELINE_SEC * 30
    assert abs(speed_est.wall_time_seconds - slow_seconds) < 1.0, \
        "wall time should track the slow slot"
    assert speed_est.wall_time_seconds > fast_seconds
    print(f"2 slots, mismatched speeds                ->  wall = slow ({speed_est.wall_time_seconds:.0f}s)   OK")

    # ------- Two slots, equal speed, unbalanced frames -------
    pair_unbal = [
        MixSlot(1.0, 1.0, 10),
        MixSlot(1.0, 1.0, 50),
    ]
    unbal = estimate_cost_for_mix(baseline, pair_unbal)
    big_seconds = BASELINE_STARTUP_SEC + BASELINE_SEC * 50
    assert abs(unbal.wall_time_seconds - big_seconds) < 1.0
    print(f"2 slots, unbalanced (10 vs 50 frames)     ->  wall = big chunk ({unbal.wall_time_seconds:.0f}s)  OK")

    # ------- Confidence band default +/-25% -------
    assert abs(single.cost_high_usd / single.cost_low_usd - 1.25 / 0.75) < 0.001
    print("default band +/-25%                         ->  high/low ratio = 5/3   OK")

    # ------- Custom band -------
    custom_band = estimate_cost_for_mix(baseline, single_mix, confidence_band=0.5)
    assert abs(custom_band.cost_high_usd - single.cost_mid_usd * 1.5) < 0.01
    assert abs(custom_band.cost_low_usd - single.cost_mid_usd * 0.5) < 0.01
    print("custom band +/-50%                          ->  applied symmetrically   OK")

    # ------- Pathological band (>1.0) clamped -------
    over_band = estimate_cost_for_mix(baseline, single_mix, confidence_band=2.0)
    # _MAX_CONFIDENCE_BAND = 0.95 → clamped
    assert abs(over_band.cost_low_usd - single.cost_mid_usd * 0.05) < 0.01
    print("band=2.0 (clamped to 0.95)                ->  cost_low not negative   OK")

    # ------- Heavier scene → higher cost (volumetrics doubles spf) -------
    heavy = dict(baseline)
    heavy["uses_volumetrics"] = True
    heavy_est = estimate_cost_for_mix(heavy, single_mix)
    # spf doubles; startup unchanged.  seconds = 90 + 60*60 = 3690s vs baseline's 1890.
    assert heavy_est.cost_mid_usd > single.cost_mid_usd
    assert heavy_est.wall_time_seconds > single.wall_time_seconds
    print(f"+ volumetrics on the scene                ->  cost up "
          f"(${single.cost_mid_usd:.2f} -> ${heavy_est.cost_mid_usd:.2f})           OK")

    # ------- Higher price → linear scaling, same wall time -------
    pricey_mix = [MixSlot(1.0, 4.0, 60)]   # 4x price
    pricey = estimate_cost_for_mix(baseline, pricey_mix)
    assert abs(pricey.cost_mid_usd - 4 * single.cost_mid_usd) < 0.01
    assert abs(pricey.wall_time_seconds - single.wall_time_seconds) < 0.01
    print("price 4x, same scene                      ->  cost 4x, wall-time same  OK")

    # ------- file_size_bytes inflates startup, hence cost AND wall time -------
    big_file = estimate_cost_for_mix(baseline, single_mix, file_size_bytes=2 * 1024 ** 3)
    # 2GB file → +60s startup
    assert big_file.wall_time_seconds > single.wall_time_seconds
    assert big_file.cost_mid_usd > single.cost_mid_usd
    expected_extra = (60 / 3600.0) * 1.0   # 60s extra * $1/hr
    assert abs((big_file.cost_mid_usd - single.cost_mid_usd) - expected_extra) < 0.01
    print(f"2GB file -> startup adds 60s              ->  wall +60s, cost +${expected_extra:.4f}   OK")

    # ------- Snapshot wrapper -------
    legacy_est = estimate_cost_from_snapshot(None, single_mix)
    assert legacy_est.cost_mid_usd > 0
    print("legacy snapshot wrapper                   ->  works                  OK")

    # ------- Sanity: 10x more frames roughly 10x cost (modulo fixed startup) -------
    small = estimate_cost_for_mix(baseline, [MixSlot(1.0, 1.0, 10)])
    big = estimate_cost_for_mix(baseline, [MixSlot(1.0, 1.0, 100)])
    # small: 90 + 30*10 = 390s.  big: 90 + 30*100 = 3090s.  ratio ~7.9x (not 10x because of fixed startup).
    ratio = big.cost_mid_usd / small.cost_mid_usd
    assert 7.0 < ratio < 9.0, f"10x frames should be ~7-9x cost (startup is fixed), got {ratio}"
    print(f"10x frames                                ->  cost {ratio:.1f}x (startup amortizes)    OK")

    # ------- Slot with 0 frames is skipped -------
    mixed_zero = [
        MixSlot(1.0, 1.0, 0),
        MixSlot(1.0, 1.0, 60),
    ]
    skipped = estimate_cost_for_mix(baseline, mixed_zero)
    assert abs(skipped.cost_mid_usd - single.cost_mid_usd) < 0.01
    assert abs(skipped.wall_time_seconds - single.wall_time_seconds) < 0.01
    print("zero-frame slot                           ->  skipped (no cost)      OK")

    # ------- Negative price clamped to 0 -------
    neg_mix = [MixSlot(1.0, -5.0, 60)]
    neg_est = estimate_cost_for_mix(baseline, neg_mix)
    assert neg_est.cost_mid_usd == 0.0
    print("negative price                            ->  clamped to 0           OK")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    _smoke()
