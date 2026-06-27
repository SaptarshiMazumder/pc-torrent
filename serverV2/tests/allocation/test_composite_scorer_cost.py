"""cost_weight folds $/chunk into the composite score.

At 0 the score is cost-blind (speed wins).  Raised, it lets a slow-cheap
card (e.g. a 4090) outrank a fast-pricey one (e.g. an H200).  Free/unknown
price stays neutral so community isn't given a runaway boost.
"""

from __future__ import annotations

from dataclasses import replace

from serverV2.allocation.allocation_strategies.analyzers.allocation_composite_scorer import (
    cost_factor,
    score_target,
)
from serverV2.tests.allocation.planner_scenarios import (
    MEDIUM_HEAVINESS,
    _default_weights,
)


def _score(*, speed: float, price: float, cost_weight: float) -> float:
    weights = replace(_default_weights(), cost_weight=cost_weight)
    return score_target(
        render_speed=speed,
        heaviness=MEDIUM_HEAVINESS,
        estimated_chunk_frames=20,
        weights=weights,
        price_per_hour=price,
    )


def test_cost_blind_default_ranks_by_speed():
    # cost_weight=0 -> the faster card wins regardless of price.
    fast_pricey = _score(speed=2.8, price=4.0, cost_weight=0.0)
    slow_cheap = _score(speed=1.5, price=0.4, cost_weight=0.0)
    assert fast_pricey > slow_cheap


def test_cost_weight_lets_cheaper_card_win():
    # With cost weighting on, the slow-cheap card overtakes the fast-pricey one.
    fast_pricey = _score(speed=2.8, price=4.0, cost_weight=1.5)
    slow_cheap = _score(speed=1.5, price=0.4, cost_weight=1.5)
    assert slow_cheap > fast_pricey


def test_free_or_unknown_price_is_neutral():
    assert cost_factor(chunk_seconds=300.0, price_per_hour=0.0) == 1.0
    assert cost_factor(chunk_seconds=300.0, price_per_hour=-5.0) == 1.0


def test_cheaper_chunk_scores_higher():
    cheap = cost_factor(chunk_seconds=300.0, price_per_hour=0.4)
    pricey = cost_factor(chunk_seconds=300.0, price_per_hour=4.0)
    assert cheap > pricey
