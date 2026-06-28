"""TaskActualCost — derive (actual_seconds, actual_cost_usd) for a chunk.

Single source of truth for the priority-adjusted cost of one chunk
(jobs row).  Used by:
  * the orchestrator facade (UI display path) -- ``actual_cost_for_chunk``
  * ``UsersClient`` -- the post-terminal credit debit
  * ``RenderLifecycle`` -- the terminal-group cost snapshot

All three go through ``actual_cost_for_chunk`` so the number the user
saw "live ticking", the amount debited from their balance, and the
value frozen on the terminal group row can never diverge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

_INFLIGHT_STATUSES = frozenset({"running", "uploading"})


class TaskActualCost:

    def __init__(
        self,
        *,
        get_priority_multiplier: Callable[[int], float],
    ) -> None:
        # Live Firestore knob, read per call so an admin tuning the
        # priority multiplier sees it reflected immediately across
        # display, billing, and the terminal cost snapshot.
        self._get_priority_multiplier = get_priority_multiplier

    def actual_cost_for_chunk(
        self,
        *,
        started_at: Any,
        completed_at: Any,
        price_per_hour: Any,
        status: str,
        priority: int,
    ) -> tuple[float | None, float | None]:
        """Priority-adjusted ``(actual_seconds, actual_cost_usd)`` for one
        chunk.  The ONE place the priority multiplier meets the rate-based
        cost -- UI display, the credit debit, and the terminal snapshot all
        call this.  Seconds are NOT multiplied (wall time is priority-
        independent).  A None / non-positive base cost passes through
        unmultiplied (worker never ran, or no rate stamped)."""
        seconds, base_cost = self.compute(
            started_at=started_at,
            completed_at=completed_at,
            price_per_hour=price_per_hour,
            status=status,
        )
        if base_cost is None or base_cost <= 0:
            return seconds, base_cost
        return seconds, base_cost * self._get_priority_multiplier(priority)

    def compute(
        self,
        *,
        started_at: Any,
        completed_at: Any,
        price_per_hour: Any,
        status: str,
    ) -> tuple[float | None, float | None]:
        """Returns ``(actual_seconds, actual_cost_usd)``.

        Rules:
          * ``started_at`` missing -> both None (worker never ran).
          * ``completed_at`` present -> use it as the end (terminal).
          * status in {running, uploading} with no completed_at -> use
            ``now`` so the UI ticks live; replaced by the canonical
            ``completed_at - started_at`` once the row flips terminal.
          * ``price_per_hour`` missing -> seconds returned, cost None
            (e.g. legacy community rows without a rate).
        """
        started = _to_datetime(started_at)
        if started is None:
            return None, None
        end = _to_datetime(completed_at)
        if end is None and status in _INFLIGHT_STATUSES:
            end = datetime.now(timezone.utc)
        if end is None:
            return None, None
        seconds = (end - started).total_seconds()
        if seconds < 0:
            seconds = 0.0
        rate = _maybe_float(price_per_hour)
        if rate is None:
            return seconds, None
        return seconds, seconds * rate / 3600.0


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
