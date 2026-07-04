"""CloudMonitoringClient — read-only Cloud Monitoring time-series via ADC.

Authenticates with Application Default Credentials (the Cloud Run runtime
service account) — NO key in env.  Needs ``roles/monitoring.viewer`` on the
compute project (`gcloud projects add-iam-policy-binding …`); until that grant
lands, ``available()`` returns False and callers show a "grant pending" state.

All Cloud Run services (dev/staging/prod) live in ONE GCP project, and
Monitoring is project-scoped, so a single query returns every service's series
— the admin dashboard can show them side by side from any environment.

Read-only: only issues ``timeSeries.list`` GETs.  ``google-auth`` ships with
``firebase-admin`` (already a dependency), so no new package is required.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/monitoring.read"
_BASE = "https://monitoring.googleapis.com/v3/projects"


class CloudMonitoringClient:

    def __init__(self, project: str | None = None) -> None:
        # Compute project that owns the Cloud Run services (shared across envs).
        self._project = (project or os.environ.get("GCP_PROJECT", "")).strip()
        self._creds = None

    def project(self) -> str:
        return self._project

    def _token(self) -> str:
        import google.auth
        import google.auth.transport.requests

        if self._creds is None:
            self._creds, adc_project = google.auth.default(scopes=[_SCOPE])
            if not self._project:
                self._project = (adc_project or "").strip()
        if not getattr(self._creds, "valid", False):
            self._creds.refresh(google.auth.transport.requests.Request())
        return self._creds.token

    def available(self) -> bool:
        """True iff ADC creds resolve and a project is known.  Never raises."""
        try:
            self._token()
            return bool(self._project)
        except Exception as exc:  # noqa: BLE001
            log.info("Cloud Monitoring unavailable (grant pending?): %s", exc)
            return False

    def timeseries(
        self,
        metric_type: str,
        start_iso: str,
        end_iso: str,
        aligner: str,
        alignment_period_sec: int,
    ) -> list[dict[str, Any]]:
        """Raw ``timeSeries`` for a metric over [start, end], per-series aligned
        (one entry per revision/resource — caller groups by service_name)."""
        token = self._token()
        params = {
            "filter": f'metric.type="{metric_type}"',
            "interval.startTime": start_iso,
            "interval.endTime": end_iso,
            "aggregation.alignmentPeriod": f"{alignment_period_sec}s",
            "aggregation.perSeriesAligner": aligner,
        }
        resp = httpx.get(
            f"{_BASE}/{self._project}/timeSeries",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("timeSeries", []) or []


def point_value(point: dict) -> float:
    v = point.get("value", {}) or {}
    if "int64Value" in v:
        try:
            return float(v["int64Value"])
        except (TypeError, ValueError):
            return 0.0
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return 0.0
    return 0.0
