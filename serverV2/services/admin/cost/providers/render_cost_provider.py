"""RenderCostProvider — GPU render spend, EXACT, from our own Postgres.

This is the dominant cost line and we own the ground truth: every completed
chunk's ``cost_actual_usd`` is recorded in ``render_telemetry`` (computed from
the fleet's price × runtime at monitor time).  So render cost needs no
external billing API — it's summed straight from the DB.

Reads only:
  * month-to-date actual spend (calendar month)
  * 30-day daily series (for the graph)
  * month-to-date breakdown by fleet
  * live in-flight cost-so-far + $/hour burn from currently-running jobs
"""

from __future__ import annotations

import logging

from serverV2.infrastructure.db import query_all, query_one
from serverV2.services.admin.cost.cost_snapshot import CONFIDENCE_EXACT, CostSnapshot

log = logging.getLogger(__name__)


class RenderCostProvider:

    name = "render"

    def snapshot(self) -> CostSnapshot:
        mtd = query_one(
            """
            SELECT COALESCE(SUM(cost_actual_usd), 0) AS usd,
                   COUNT(*) AS chunks
              FROM render_telemetry
             WHERE completed_at >= date_trunc('month', now())
            """,
        ) or {}

        series = query_all(
            """
            SELECT DATE(completed_at) AS day,
                   COALESCE(SUM(cost_actual_usd), 0) AS usd
              FROM render_telemetry
             WHERE completed_at >= now() - interval '30 days'
             GROUP BY 1
             ORDER BY 1
            """,
        )

        by_fleet = query_all(
            """
            SELECT COALESCE(fleet, 'unknown') AS fleet,
                   COALESCE(SUM(cost_actual_usd), 0) AS usd
              FROM render_telemetry
             WHERE completed_at >= date_trunc('month', now())
             GROUP BY 1
             ORDER BY usd DESC
            """,
        )

        # Live: jobs still running.  Cost-so-far = price/hr × elapsed; burn =
        # sum of their price/hr.  elapsed computed in-SQL (timezone-safe).
        live = query_one(
            """
            SELECT COUNT(*) AS running,
                   COALESCE(SUM(price_per_hour_at_dispatch), 0) AS burn_per_hr,
                   COALESCE(SUM(
                       price_per_hour_at_dispatch
                       * EXTRACT(EPOCH FROM (now() - started_at)) / 3600.0
                   ), 0) AS inflight_usd
              FROM jobs
             WHERE status = 'running'
               AND started_at IS NOT NULL
               AND price_per_hour_at_dispatch IS NOT NULL
            """,
        ) or {}

        mtd_usd = float(mtd.get("usd") or 0.0)
        inflight = float(live.get("inflight_usd") or 0.0)
        return CostSnapshot(
            name=self.name,
            label="GPU render (Vast / Modal / community)",
            confidence=CONFIDENCE_EXACT,
            # Actuals booked this month PLUS what's accruing on running jobs.
            month_to_date_usd=round(mtd_usd + inflight, 4),
            live_rate_usd_per_hr=round(float(live.get("burn_per_hr") or 0.0), 4),
            usage={
                "chunks_this_month": int(mtd.get("chunks") or 0),
                "running_jobs": int(live.get("running") or 0),
                "inflight_usd": round(inflight, 4),
                "booked_this_month_usd": round(mtd_usd, 4),
            },
            series=[{"day": str(r["day"]), "usd": float(r["usd"] or 0)} for r in series],
            breakdown=[{"key": r["fleet"], "usd": float(r["usd"] or 0)} for r in by_fleet],
            note="Exact — summed from render_telemetry (our own records) + live in-flight accrual.",
        )
