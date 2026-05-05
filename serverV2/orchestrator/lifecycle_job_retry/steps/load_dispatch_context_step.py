"""LoadDispatchContextStep — shared between auto and manual pipelines.

Populates ``ctx.engine`` and ``ctx.tier`` from the group row.  Engine
comes from ``resolved_scene_json.heaviness.render_engine`` (single
source of truth post-submit); tier comes straight off the row.

# SOURCE: retry_dispatcher.py:234-268 (legacy load_group_dispatch_context)
"""

from __future__ import annotations

import json
from typing import Any

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class LoadDispatchContextStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        ctx.engine, ctx.tier = self._load(ctx.grp)

    @staticmethod
    def _load(
        grp: dict[str, Any] | None,
    ) -> tuple[str | None, str | None]:
        engine: str | None = None
        tier: str | None = None
        if grp is None:
            return engine, tier

        raw_scene = grp.get("resolved_scene_json")
        if raw_scene:
            parsed = json.loads(raw_scene)
            if isinstance(parsed, dict):
                heaviness = parsed.get("heaviness")
                if isinstance(heaviness, dict):
                    engine_value = heaviness.get("render_engine")
                    if isinstance(engine_value, str) and engine_value:
                        engine = engine_value

        tier_raw = grp.get("tier")
        if isinstance(tier_raw, str):
            tier = tier_raw

        return engine, tier
