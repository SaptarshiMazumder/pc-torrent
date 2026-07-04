"""Cost accounting — the value object every source returns, plus the
provider protocol they all implement.

Design mirrors the app's strategy/protocol pattern (FleetStrategy,
AllocationTarget): each paid dependency has a ``CostSourceProvider`` that
reads ONLY its own source and returns a uniform ``CostSnapshot``.  The
``CostAccountingService`` fans out over them and aggregates.  Every provider
is read-only and fail-open — a dead source degrades its card, never the page.

``confidence`` tells the UI whether a dollar figure is ground truth
(``exact`` — e.g. GPU spend summed from our own render_telemetry) or a
usage×price projection (``estimated`` — e.g. Redis commands × Upstash rate).
A provider flips itself to ``exact`` automatically once a billing key is
present in the environment; until then it estimates and says so in ``note``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

CONFIDENCE_EXACT = "exact"
CONFIDENCE_ESTIMATED = "estimated"
CONFIDENCE_UNAVAILABLE = "unavailable"


@dataclass
class CostSnapshot:
    """One cost source's current picture.  All dollar fields are USD."""

    name: str                                  # stable slug: "render", "redis"
    label: str                                 # human: "GPU render", "Redis (Upstash)"
    confidence: str = CONFIDENCE_ESTIMATED     # exact | estimated | unavailable
    # Actual spend accrued so far this calendar month (only sources we can
    # count exactly — e.g. render).  None when we can only project.
    month_to_date_usd: float | None = None
    # Run-rate projection for the full month (estimated sources).  None when
    # not projectable.
    projected_monthly_usd: float | None = None
    # Instantaneous burn, USD/hour (e.g. sum of running instances' price).
    live_rate_usd_per_hr: float | None = None
    # Raw source-native usage (commands, bytes, instance-hours) for the card.
    usage: dict[str, Any] = field(default_factory=dict)
    # Time series for graphs: ``[{"day": "2026-07-01", "usd": 1.23}, ...]``.
    series: list[dict[str, Any]] = field(default_factory=list)
    # Sub-breakdown for a bar chart: ``[{"key": "vast", "usd": 9.9}, ...]``.
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    # One line explaining the number / what unlocks exact billing.
    note: str = ""

    def best_monthly_usd(self) -> float:
        """The figure the overview should sum: actuals if we have them, else
        the projection, else 0."""
        if self.month_to_date_usd is not None:
            return self.month_to_date_usd
        if self.projected_monthly_usd is not None:
            return self.projected_monthly_usd
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "confidence": self.confidence,
            "month_to_date_usd": self.month_to_date_usd,
            "projected_monthly_usd": self.projected_monthly_usd,
            "live_rate_usd_per_hr": self.live_rate_usd_per_hr,
            "usage": self.usage,
            "series": self.series,
            "breakdown": self.breakdown,
            "note": self.note,
        }


@runtime_checkable
class CostSourceProvider(Protocol):
    """A single billable dependency's cost reader.  Read-only, fail-open."""

    name: str

    def snapshot(self) -> CostSnapshot:
        """Return the source's current cost picture.  Must not raise for
        expected failure (dead source); return an ``unavailable`` snapshot
        instead.  The service still guards with a try/except as a backstop."""
        ...
