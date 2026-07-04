"""CloudRunCostProvider — backend hosting cost ESTIMATE.

Cloud Run bills per vCPU-second + GiB-second + requests, exposed only via GCP
billing / Cloud Monitoring (a separate credential).  With no billing key here,
we estimate from an assumed always-on shape held in ``config/cost_pricing``
(vCPUs, GiB, avg instances, hours) so you get a ballpark you can tune.  Add
GCP billing (``GCP_BILLING_ACCOUNT``) to replace this with exact spend.
Read-only; no writes.
"""

from __future__ import annotations

import os
from typing import Callable

from serverV2.services.admin.cost.cost_pricing_config import CostPricingConfig
from serverV2.services.admin.cost.cost_snapshot import CONFIDENCE_ESTIMATED, CostSnapshot


class CloudRunCostProvider:

    name = "cloudrun"

    def __init__(self, get_pricing: Callable[[], CostPricingConfig]) -> None:
        self._get_pricing = get_pricing

    def snapshot(self) -> CostSnapshot:
        p = self._get_pricing()
        inst = p.cloudrun_avg_instances
        hrs = p.cloudrun_hours_per_month
        vcpu_cost = inst * p.cloudrun_vcpus * p.cloudrun_usd_per_vcpu_hour * hrs
        mem_cost = inst * p.cloudrun_gib * p.cloudrun_usd_per_gib_hour * hrs
        total = vcpu_cost + mem_cost

        has_key = bool(os.environ.get("GCP_BILLING_ACCOUNT", "").strip())
        return CostSnapshot(
            name=self.name,
            label="Cloud Run (backend hosting)",
            confidence=CONFIDENCE_ESTIMATED,
            projected_monthly_usd=round(total, 2),
            usage={
                "assumed_avg_instances": inst,
                "vcpus": p.cloudrun_vcpus,
                "gib": p.cloudrun_gib,
                "hours_per_month": hrs,
            },
            breakdown=[
                {"key": "vCPU", "usd": round(vcpu_cost, 2)},
                {"key": "memory", "usd": round(mem_cost, 2)},
            ],
            note=(
                "Rough estimate from an assumed always-on shape — tune "
                "cloudrun_* in config/cost_pricing.  See the Cloud (GCP) tab "
                "for real usage-based cost per service from Cloud Monitoring."
                + ("" if has_key else "  Add GCP_BILLING_ACCOUNT for exact billing.")
            ),
        )
