"""TaskActualCost — derive (actual_seconds, actual_cost_usd) from a jobs row.

Pure stateless helper.  Used by:
  * the orchestrator facade (UI display path via ``actual_cost_for_row``)
  * ``UsersClient`` for the post-terminal credit debit

Same formula either way -- single source of truth so the number the
user saw "live ticking" matches the number that comes off their
credit balance when the chunk lands.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_INFLIGHT_STATUSES = frozenset({"running", "uploading"})


class TaskActualCost:

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
