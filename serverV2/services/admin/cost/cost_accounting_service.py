"""CostAccountingService — read-only fan-out over the cost source providers.

Backs the admin dashboard's cost overview (summary of every paid dependency)
and the per-source drill-down.  Each provider is isolated: one failing source
yields an ``unavailable`` card, never a 500.  Zero writes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from serverV2.services.admin.cost.cost_snapshot import (
    CONFIDENCE_UNAVAILABLE,
    CostSnapshot,
    CostSourceProvider,
)

log = logging.getLogger(__name__)


class CostAccountingService:

    def __init__(self, providers: list[CostSourceProvider]) -> None:
        self._providers = list(providers)

    def _safe_snapshot(self, provider: CostSourceProvider) -> CostSnapshot:
        name = getattr(provider, "name", "?")
        try:
            return provider.snapshot()
        except Exception as exc:  # noqa: BLE001 — one dead source must not 500 the page
            log.warning("Cost provider %s failed: %s", name, exc)
            return CostSnapshot(
                name=name, label=name, confidence=CONFIDENCE_UNAVAILABLE,
                note=f"read failed: {exc}",
            )

    def overview(self) -> dict[str, Any]:
        snaps = [self._safe_snapshot(p) for p in self._providers]
        total_month = sum(s.best_monthly_usd() for s in snaps)
        total_live = sum(s.live_rate_usd_per_hr or 0.0 for s in snaps)
        ordered = sorted(snaps, key=lambda s: s.best_monthly_usd(), reverse=True)
        return {
            "total_month_to_date_usd": round(total_month, 4),
            "total_live_rate_usd_per_hr": round(total_live, 6),
            "sources": [s.to_dict() for s in ordered],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def source(self, name: str) -> dict[str, Any] | None:
        for provider in self._providers:
            if getattr(provider, "name", None) == name:
                return self._safe_snapshot(provider).to_dict()
        return None
