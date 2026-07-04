"""PostgresCostProvider — Neon storage cost ESTIMATE from DB size.

Read-only: ``pg_database_size`` + per-table sizes turn into a storage estimate
via the tunable ``neon_usd_per_gb_month`` rate.  Neon also bills compute-hours,
which need the Neon API (``NEON_API_KEY``) to read exactly; storage alone is
estimable from what the DB already reports about itself.  No writes.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from serverV2.infrastructure.db import query_all, query_one
from serverV2.services.admin.cost.cost_pricing_config import CostPricingConfig
from serverV2.services.admin.cost.cost_snapshot import (
    CONFIDENCE_ESTIMATED,
    CONFIDENCE_UNAVAILABLE,
    CostSnapshot,
)

log = logging.getLogger(__name__)


class PostgresCostProvider:

    name = "postgres"

    def __init__(self, get_pricing: Callable[[], CostPricingConfig]) -> None:
        self._get_pricing = get_pricing

    def snapshot(self) -> CostSnapshot:
        try:
            size = query_one("SELECT pg_database_size(current_database()) AS bytes") or {}
            tables = query_all(
                """
                SELECT relname AS name,
                       pg_total_relation_size(relid) AS bytes
                  FROM pg_catalog.pg_statio_user_tables
                 ORDER BY bytes DESC
                 LIMIT 15
                """,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Postgres cost size query failed: %s", exc)
            return CostSnapshot(
                name=self.name, label="Postgres (Neon)",
                confidence=CONFIDENCE_UNAVAILABLE, note="DB size query failed.",
            )

        p = self._get_pricing()
        total_bytes = int(size.get("bytes") or 0)
        gb = total_bytes / 1_000_000_000
        storage_cost = gb * p.neon_usd_per_gb_month

        has_key = bool(os.environ.get("NEON_API_KEY", "").strip())
        return CostSnapshot(
            name=self.name,
            label="Postgres (Neon)",
            confidence=CONFIDENCE_ESTIMATED,
            projected_monthly_usd=round(storage_cost, 4),
            usage={"db_size_bytes": total_bytes, "db_size_gb": round(gb, 4)},
            breakdown=[
                {"key": t["name"], "usd": round((int(t["bytes"] or 0) / 1_000_000_000) * p.neon_usd_per_gb_month, 6),
                 "bytes": int(t["bytes"] or 0)}
                for t in tables
            ],
            note=(
                f"Estimated storage: {gb:.3f} GB × ${p.neon_usd_per_gb_month:g}/GB-mo. "
                "Compute-hours billed separately"
                + ("." if has_key else " — add NEON_API_KEY for exact compute + storage billing.")
            ),
        )
