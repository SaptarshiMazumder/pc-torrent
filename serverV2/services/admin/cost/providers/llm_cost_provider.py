"""LlmCostProvider — Anthropic/OpenAI spend from recorded token usage.

Reads the ``llm_usage`` table (populated fail-open by LLMFacade) and multiplies
month-to-date tokens by the configured per-token prices.  Tokens are our own
exact counts; the price is a tunable constant, so this reads ``estimated`` (it
becomes exact once you set per-model prices in config/cost_pricing that match
your contract).  Read-only.
"""

from __future__ import annotations

from typing import Callable

from serverV2.services.admin.cost.cost_pricing_config import CostPricingConfig
from serverV2.services.admin.cost.cost_snapshot import CONFIDENCE_ESTIMATED, CostSnapshot


class LlmCostProvider:

    name = "llm"

    def __init__(self, usage_repo, get_pricing: Callable[[], CostPricingConfig]) -> None:
        self._usage = usage_repo
        self._get_pricing = get_pricing

    def snapshot(self) -> CostSnapshot:
        summary = self._usage.summary() or {}
        mtd = summary.get("month_to_date") or {}
        p = self._get_pricing()

        in_tok = int(mtd.get("input_tokens") or 0)
        out_tok = int(mtd.get("output_tokens") or 0)
        in_cost = in_tok * p.llm_default_usd_per_input_token
        out_cost = out_tok * p.llm_default_usd_per_output_token
        total = in_cost + out_cost

        by_model = [
            {
                "key": f"{m.get('provider')}/{m.get('model')}",
                "usd": round(
                    int(m.get("input_tokens") or 0) * p.llm_default_usd_per_input_token
                    + int(m.get("output_tokens") or 0) * p.llm_default_usd_per_output_token,
                    4,
                ),
            }
            for m in (summary.get("by_model") or [])
        ]
        series = [
            {
                "day": str(d.get("day")),
                "usd": round(
                    int(d.get("input_tokens") or 0) * p.llm_default_usd_per_input_token
                    + int(d.get("output_tokens") or 0) * p.llm_default_usd_per_output_token,
                    4,
                ),
            }
            for d in (summary.get("by_day") or [])
        ]

        return CostSnapshot(
            name=self.name,
            label="LLM (Anthropic / OpenAI)",
            confidence=CONFIDENCE_ESTIMATED,
            month_to_date_usd=round(total, 4),
            usage={
                "calls_this_month": int(mtd.get("calls") or 0),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            },
            series=series,
            breakdown=by_model,
            note=(
                f"From recorded tokens × configured price "
                f"(${p.llm_default_usd_per_input_token * 1_000_000:g}/M in, "
                f"${p.llm_default_usd_per_output_token * 1_000_000:g}/M out). "
                "Set per-model prices in config/cost_pricing to match your contract."
            ),
        )
