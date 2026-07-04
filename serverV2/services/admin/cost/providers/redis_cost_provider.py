"""RedisCostProvider — Upstash cost ESTIMATE from Redis' own INFO.

Read-only: one ``INFO`` (the server's global counters across every instance +
the cron) turns into a usage×price estimate.  No writes, no new keys.  If you
add an Upstash management token (``UPSTASH_MGMT_TOKEN``) later, this provider
can be upgraded to read exact billed usage; until then it projects from the
instantaneous op rate and reports ``estimated``.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from serverV2.infrastructure.redis_client import RedisClient
from serverV2.services.admin.cost.cost_pricing_config import CostPricingConfig
from serverV2.services.admin.cost.cost_snapshot import (
    CONFIDENCE_ESTIMATED,
    CONFIDENCE_UNAVAILABLE,
    CostSnapshot,
)

log = logging.getLogger(__name__)

_SECONDS_PER_MONTH = 60 * 60 * 24 * 30


class RedisCostProvider:

    name = "redis"

    def __init__(
        self,
        redis_client: RedisClient,
        get_pricing: Callable[[], CostPricingConfig],
    ) -> None:
        self._redis = redis_client
        self._get_pricing = get_pricing

    def snapshot(self) -> CostSnapshot:
        client = self._redis.client()
        if client is None:
            return CostSnapshot(
                name=self.name, label="Redis (Upstash)",
                confidence=CONFIDENCE_UNAVAILABLE,
                note="Redis not connected on this instance.",
            )
        try:
            info = client.info()
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis cost INFO failed: %s", exc)
            return CostSnapshot(
                name=self.name, label="Redis (Upstash)",
                confidence=CONFIDENCE_UNAVAILABLE, note="Redis INFO unavailable.",
            )
        try:
            dbsize = client.dbsize()
        except Exception:
            dbsize = None

        p = self._get_pricing()
        ops = float(info.get("instantaneous_ops_per_sec") or 0.0)
        total_cmds = int(info.get("total_commands_processed") or 0)
        mem_bytes = int(info.get("used_memory") or 0)
        mem_gb = mem_bytes / 1_000_000_000

        monthly_cmds = ops * _SECONDS_PER_MONTH
        cmd_cost = monthly_cmds * p.redis_usd_per_command
        storage_cost = mem_gb * p.redis_usd_per_gb_month
        projected = cmd_cost + storage_cost

        has_key = bool(os.environ.get("UPSTASH_MGMT_TOKEN", "").strip())
        return CostSnapshot(
            name=self.name,
            label="Redis (Upstash)",
            confidence=CONFIDENCE_ESTIMATED,
            projected_monthly_usd=round(projected, 4),
            live_rate_usd_per_hr=round(ops * 3600 * p.redis_usd_per_command, 6),
            usage={
                "ops_per_sec": ops,
                "total_commands_processed": total_cmds,
                "used_memory_bytes": mem_bytes,
                "used_memory_human": info.get("used_memory_human"),
                "dbsize": dbsize,
                "connected_clients": info.get("connected_clients"),
            },
            breakdown=[
                {"key": "commands (projected/mo)", "usd": round(cmd_cost, 4)},
                {"key": "storage", "usd": round(storage_cost, 4)},
            ],
            note=(
                f"Estimated: {ops:g} ops/s → ~{monthly_cmds:,.0f} cmds/mo × "
                f"${p.redis_usd_per_command:g} + {mem_gb:.3f} GB × "
                f"${p.redis_usd_per_gb_month:g}/mo."
                + ("" if has_key else "  Add UPSTASH_MGMT_TOKEN for exact billed usage.")
            ),
        )
