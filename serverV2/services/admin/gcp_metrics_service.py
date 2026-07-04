"""GcpMetricsService — Cloud Run usage + cost, per service, from Monitoring.

Read-only.  Pulls request count, billable instance time, and instance count for
every Cloud Run service in the shared compute project (dev/staging/prod), as a
daily series for graphs plus month totals.  Cost per service is estimated from
real billable-instance-time × the tunable Cloud Run price (far better than an
assumed shape).  No server-side cache — the UI slow-polls (30 min) and offers a
Refresh button, which is the rate limit; a manual refresh fetches fresh.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from serverV2.infrastructure.gcp.cloud_monitoring import CloudMonitoringClient, point_value
from serverV2.services.admin.cost.cost_pricing_config import CostPricingConfig

log = logging.getLogger(__name__)

_REQUEST_COUNT = "run.googleapis.com/request_count"
_BILLABLE_TIME = "run.googleapis.com/container/billable_instance_time"
_INSTANCE_COUNT = "run.googleapis.com/container/instance_count"


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


class GcpMetricsService:

    def __init__(
        self,
        client: CloudMonitoringClient,
        get_pricing: Callable[[], CostPricingConfig],
    ) -> None:
        self._client = client
        self._get_pricing = get_pricing

    def metrics(self, days: int = 30) -> dict[str, Any]:
        days = max(1, min(int(days), 90))
        if not self._client.available():
            return {
                "available": False,
                "note": (
                    "Grant roles/monitoring.viewer to the Cloud Run runtime "
                    "service account on the compute project, then refresh — no "
                    "key needed. See serverV2/services/admin/cost/README.md."
                ),
            }
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        try:
            req = self._client.timeseries(_REQUEST_COUNT, _iso(start), _iso(end), "ALIGN_DELTA", 86400)
            billable = self._client.timeseries(_BILLABLE_TIME, _iso(start), _iso(end), "ALIGN_DELTA", 86400)
            instances = self._client.timeseries(_INSTANCE_COUNT, _iso(start), _iso(end), "ALIGN_MEAN", 86400)
        except Exception as exc:  # noqa: BLE001
            log.warning("GCP metrics query failed: %s", exc)
            return {"available": False, "error": str(exc),
                    "note": "Monitoring query failed — check the monitoring.viewer grant."}

        services: dict[str, dict[str, dict[str, float]]] = {}

        def ingest(series_list: list[dict], key: str) -> None:
            for s in series_list:
                svc = ((s.get("resource") or {}).get("labels") or {}).get("service_name") or "unknown"
                bucket = services.setdefault(svc, {"requests": {}, "billable_seconds": {}, "instances": {}})
                for pt in s.get("points", []) or []:
                    day = ((pt.get("interval") or {}).get("endTime") or "")[:10]
                    if not day:
                        continue
                    bucket[key][day] = bucket[key].get(day, 0.0) + point_value(pt)

        ingest(req, "requests")
        ingest(billable, "billable_seconds")
        ingest(instances, "instances")

        p = self._get_pricing()
        per_sec = (
            p.cloudrun_vcpus * p.cloudrun_usd_per_vcpu_hour / 3600.0
            + p.cloudrun_gib * p.cloudrun_usd_per_gib_hour / 3600.0
        )

        def series(d: dict[str, float]) -> list[dict]:
            return [{"day": k, "value": round(v, 2)} for k, v in sorted(d.items())]

        out = []
        for svc, b in services.items():
            req_total = sum(b["requests"].values())
            bill_total = sum(b["billable_seconds"].values())
            out.append({
                "service": svc,
                "requests_total": int(req_total),
                "billable_seconds_total": round(bill_total),
                "est_cost_usd": round(bill_total * per_sec, 2),
                "requests_series": series(b["requests"]),
                "billable_series": series(b["billable_seconds"]),
                "instances_series": series(b["instances"]),
            })
        out.sort(key=lambda s: s["est_cost_usd"], reverse=True)
        return {
            "available": True,
            "project": self._client.project(),
            "days": days,
            "total_est_cost_usd": round(sum(s["est_cost_usd"] for s in out), 2),
            "total_requests": sum(s["requests_total"] for s in out),
            "services": out,
            "generated_at": _iso(end),
        }
