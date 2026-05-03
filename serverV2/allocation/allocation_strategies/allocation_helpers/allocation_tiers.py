"""Tier constants — user-visible allocation tiers.

Used by ``RenderLifecycle._pick_strategy`` to route a render to the
matching allocator and budget.  See tiered_allocation_plan.md for the
full design.
"""

from __future__ import annotations


ECONOMY = "economy"
STANDARD = "standard"
PREMIUM = "premium"     # reserved — UI shows "Coming soon", allocator not built yet

_VALID = {ECONOMY, STANDARD, PREMIUM}
_DEFAULT = STANDARD


def normalize(tier: str | None) -> str:
    """Return a known tier name; default to STANDARD for None / unknown / empty."""
    if isinstance(tier, str):
        v = tier.strip().lower()
        if v in _VALID:
            return v
    return _DEFAULT


def is_implemented(tier: str) -> bool:
    """Return True if the tier has an allocator wired up.  Premium is
    reserved but not yet implemented; the lifecycle falls back to
    STANDARD if a Premium request slips through."""
    return tier in (ECONOMY, STANDARD)
