"""USD <-> credits conversion -- pure math.

Single source of truth for projecting internal USD-denominated cost
numbers into the credit currency the UI displays.  No state, no IO,
no dependencies on other modules.  Callers pass the rate explicitly
so the function stays a one-liner of arithmetic.
"""

from __future__ import annotations


def usd_to_credits(usd: float | None, credits_per_usd: float) -> float | None:
    """Convert a USD value into credits.  ``None`` passes through so
    callers can hand off "no data" without special-casing.
    """
    if usd is None:
        return None
    return float(usd) * float(credits_per_usd)
