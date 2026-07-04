"""R2CostProvider — Cloudflare R2 storage cost ESTIMATE from object bytes.

Read-only: lists the bucket (S3 API, the creds already in env) and sums object
sizes → storage estimate.  R2 egress is free, so storage is the whole bill.
The listing is cached in-memory (1h) and page-capped so it never runs on the
hot admin poll and its own Class-B list cost stays negligible.  Add a
Cloudflare API token (``CLOUDFLARE_API_TOKEN``) later for exact billed usage.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from serverV2.services.admin.cost.cost_pricing_config import CostPricingConfig
from serverV2.services.admin.cost.cost_snapshot import (
    CONFIDENCE_ESTIMATED,
    CONFIDENCE_UNAVAILABLE,
    CostSnapshot,
)

log = logging.getLogger(__name__)

_CACHE_TTL_SEC = 3600.0
_MAX_PAGES = 20          # 20 × 1000 objects; caps list cost + latency
_PAGE_SIZE = 1000


class R2CostProvider:

    name = "r2"

    def __init__(self, get_pricing: Callable[[], CostPricingConfig]) -> None:
        self._get_pricing = get_pricing
        self._cache: tuple[float, int, int, bool] | None = None  # (ts, bytes, count, truncated)

    def _sized(self) -> tuple[int, int, bool] | None:
        now = time.time()
        if self._cache is not None and (now - self._cache[0]) < _CACHE_TTL_SEC:
            return self._cache[1], self._cache[2], self._cache[3]
        try:
            from serverV2.infrastructure.storage.client import get_bucket, get_s3_client
            s3 = get_s3_client()
            bucket = get_bucket()
            total = 0
            count = 0
            token = None
            pages = 0
            truncated = False
            while True:
                kwargs = {"Bucket": bucket, "MaxKeys": _PAGE_SIZE}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = s3.list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []) or []:
                    total += int(obj.get("Size") or 0)
                    count += 1
                pages += 1
                if resp.get("IsTruncated") and pages < _MAX_PAGES:
                    token = resp.get("NextContinuationToken")
                    continue
                truncated = bool(resp.get("IsTruncated")) and pages >= _MAX_PAGES
                break
            self._cache = (now, total, count, truncated)
            return total, count, truncated
        except Exception as exc:  # noqa: BLE001
            log.warning("R2 sizing failed: %s", exc)
            return None

    def snapshot(self) -> CostSnapshot:
        sized = self._sized()
        if sized is None:
            return CostSnapshot(
                name=self.name, label="Cloudflare R2 (storage)",
                confidence=CONFIDENCE_UNAVAILABLE,
                note="R2 not configured or bucket listing failed.",
            )
        total_bytes, count, truncated = sized
        p = self._get_pricing()
        gb = total_bytes / 1_000_000_000
        cost = gb * p.r2_usd_per_gb_month
        return CostSnapshot(
            name=self.name,
            label="Cloudflare R2 (storage)",
            confidence=CONFIDENCE_ESTIMATED,
            projected_monthly_usd=round(cost, 4),
            usage={
                "object_count": count,
                "bytes": total_bytes,
                "gb": round(gb, 4),
                "listing_capped": truncated,
                "cached_1h": True,
            },
            note=(
                f"Estimated storage: {gb:.3f} GB × ${p.r2_usd_per_gb_month:g}/GB-mo (egress free)."
                + (" Listing page-capped — approximate." if truncated else "")
                + " Add CLOUDFLARE_API_TOKEN for exact billed usage."
            ),
        )
