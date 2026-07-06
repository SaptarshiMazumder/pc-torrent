"""cost_weight folds $/chunk into the composite score.

The cost factor is bounded to 0..1 (like cuda/os), so cost nudges the
ranking without ever burying speed.  At cost_weight 0 the score is
cost-blind (speed wins); at a realistic weight a clearly-faster card still
beats a dirt-cheap-weak one; cranked high, a slow-cheap card can overtake a
fast-pricey one.  Free/unknown price scores 1.0 (the cheapest end).
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


def test_moderate_cost_weight_keeps_speed_dominant():
    # The regression this guards: with the OLD unbounded cost_factor a
    # dirt-cheap weak card's cost score spiked high enough to beat a much
    # faster card even at a modest cost_weight.  Bounded to 0..1 it can't --
    # at a realistic cost_weight (0.2) the clearly-faster card wins even when
    # the slow one is dirt cheap.
    fast_normal = _score(speed=2.8, price=1.0, cost_weight=0.2)
    slow_dirt_cheap = _score(speed=1.0, price=0.02, cost_weight=0.2)
    assert fast_normal > slow_dirt_cheap


def test_cost_factor_is_bounded_0_1():
    # Bounding is what stops cost from dominating speed: no price, however
    # cheap, can push the factor past 1.0, and a very pricey chunk sinks
    # below the 0.5 midpoint toward 0.
    dirt_cheap = cost_factor(chunk_seconds=3600.0, price_per_hour=0.001)
    assert 0.0 < dirt_cheap <= 1.0
    very_pricey = cost_factor(chunk_seconds=3600.0, price_per_hour=100.0)
    assert 0.0 < very_pricey < 0.5


def test_free_or_unknown_price_scores_one():
    assert cost_factor(chunk_seconds=300.0, price_per_hour=0.0) == 1.0
    assert cost_factor(chunk_seconds=300.0, price_per_hour=-5.0) == 1.0


def test_cheaper_chunk_scores_higher():
    cheap = cost_factor(chunk_seconds=300.0, price_per_hour=0.4)
    pricey = cost_factor(chunk_seconds=300.0, price_per_hour=4.0)
    assert cheap > pricey
