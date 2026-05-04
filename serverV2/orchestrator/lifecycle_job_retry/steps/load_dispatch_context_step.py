"""LoadDispatchContextStep — shared between auto and manual pipelines.

Populates ``ctx.file_size_bytes``, ``ctx.engine``, ``ctx.tier`` from the
group row's ``resolved_scene_json``.  Single source of truth for engine
post-submit; no merging at the call site.

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
        ctx.file_size_bytes, ctx.engine, ctx.tier = self._load(ctx.grp)

    @staticmethod
    def _load(
        grp: dict[str, Any] | None,
    ) -> tuple[int | None, str | None, str | None]:
        file_size_bytes: int | None = None
        engine: str | None = None
        tier: str | None = None
        if grp is None:
            return file_size_bytes, engine, tier

        raw_size = grp.get("r2_input_size_bytes")
        if raw_size is not None:
            try:
                file_size_bytes = int(raw_size)
            except (TypeError, ValueError):
                file_size_bytes = None

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

        return file_size_bytes, engine, tier
