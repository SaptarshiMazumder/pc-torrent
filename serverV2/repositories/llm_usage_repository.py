"""LlmUsageRepository — append-only record of LLM token usage.

One row per LLM call (provider, model, in/out tokens).  ``record`` is
fail-open: a DB blip drops the row, never breaks the LLM call it's observing.
``summary`` aggregates for the admin cost dashboard's LLM provider.  Nothing
in the render/allocation path reads this table, so it is pure observability.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from serverV2.infrastructure.db import execute, query_all, query_one

log = logging.getLogger(__name__)


class LlmUsageRepository:

    def record(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
        try:
            execute(
                """
                INSERT INTO llm_usage (id, provider, model, input_tokens, output_tokens)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (uuid4().hex, provider or "", model or "", int(input_tokens or 0), int(output_tokens or 0)),
            )
        except Exception as exc:  # noqa: BLE001 — must never break the LLM call
            log.warning("LlmUsage.record failed: %s", exc)

    def summary(self) -> dict[str, Any]:
        """Month-to-date totals, per-model breakdown, and a 30-day daily token
        series.  Empty-safe on any error."""
        try:
            mtd = query_one(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                  FROM llm_usage
                 WHERE occurred_at >= date_trunc('month', now())
                """,
            ) or {}
            by_model = query_all(
                """
                SELECT provider, model,
                       COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                  FROM llm_usage
                 WHERE occurred_at >= date_trunc('month', now())
                 GROUP BY provider, model
                 ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC
                """,
            )
            by_day = query_all(
                """
                SELECT DATE(occurred_at) AS day,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                  FROM llm_usage
                 WHERE occurred_at >= now() - interval '30 days'
                 GROUP BY 1
                 ORDER BY 1
                """,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("LlmUsage.summary failed: %s", exc)
            return {}
        return {"month_to_date": dict(mtd), "by_model": by_model, "by_day": by_day}
