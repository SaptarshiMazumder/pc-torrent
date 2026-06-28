"""TaskActualCost.actual_cost_for_chunk — the single per-chunk cost authority.

Guards the consolidation that routed the orchestrator (UI display), UsersClient
(billing), and the terminal-snapshot rollup all through this one method: the
priority multiplier hits the cost (not the seconds), and a None / no-rate base
passes through unmultiplied so a multiplier can never conjure cost out of a
chunk that never ran.
"""

from __future__ import annotations

from serverV2.orchestrator.task_actual_cost import TaskActualCost

# 1h apart -> at $10/h the raw (pre-multiplier) cost is exactly $10.
START = "2026-01-01T00:00:00+00:00"
END_1H = "2026-01-01T01:00:00+00:00"


def _cost(multiplier: float = 1.0) -> TaskActualCost:
    return TaskActualCost(get_priority_multiplier=lambda _p: multiplier)


def test_priority_multiplier_hits_cost_not_seconds():
    seconds, usd = _cost(multiplier=2.0).actual_cost_for_chunk(
        started_at=START, completed_at=END_1H,
        price_per_hour=10.0, status="done", priority=3,
    )
    assert seconds == 3600.0   # wall time is priority-independent
    assert usd == 20.0         # $10 base * 2.0


def test_equals_compute_times_multiplier():
    # The exact equivalence UsersClient + the orchestrator inlined before
    # consolidation: actual_cost_for_chunk == compute().cost * multiplier.
    c = _cost(multiplier=1.5)
    _, base = c.compute(
        started_at=START, completed_at=END_1H, price_per_hour=10.0, status="done",
    )
    _, adjusted = c.actual_cost_for_chunk(
        started_at=START, completed_at=END_1H,
        price_per_hour=10.0, status="done", priority=2,
    )
    assert adjusted == base * 1.5


def test_never_ran_passes_through_none():
    # No started_at -> (None, None); the multiplier must not fabricate cost.
    seconds, usd = _cost(multiplier=2.0).actual_cost_for_chunk(
        started_at=None, completed_at=None,
        price_per_hour=10.0, status="pending", priority=3,
    )
    assert seconds is None
    assert usd is None


def test_missing_rate_yields_seconds_but_no_cost():
    # Legacy / community row with no rate stamped -> seconds known, cost None.
    seconds, usd = _cost(multiplier=2.0).actual_cost_for_chunk(
        started_at=START, completed_at=END_1H,
        price_per_hour=None, status="done", priority=3,
    )
    assert seconds == 3600.0
    assert usd is None
