"""ManualRetryResolveLatestSiblingStep — manual-retry pipeline.

Caller may have passed an older attempt's job_id; manual retry always
operates on the *latest* attempt for status / dispatch decisions.
Frame-range computation goes through ``ComputeRemainingFramesStep``
which considers the union of outputs across ALL sibling attempts.

Raises:
* ``not_found`` if the chunk has no sibling rows at all (shouldn't
  happen in practice — caller already validated raw exists).
* ``not_retryable`` if the latest sibling is in any non-terminal-failure
  status (``done``, ``pending``, ``running``).  Both ``failed`` and
  ``cancelled`` are accepted -- the user clicking Retry is an explicit
  intent declaration that overrides the prior cancel/failure decision.

Populates ``ctx.rj`` from the resolved latest sibling.
"""

from __future__ import annotations

from serverV2.core.models import RenderJob
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)
from serverV2.orchestrator.lifecycle_job_retry.manual_retry_error import (
    ManualRetryError,
)


class ManualRetryResolveLatestSiblingStep:

    def run(self, ctx: RetryContext) -> None:
        siblings = [
            j for j in ctx.deps.job_repo.get_raw_by_group(ctx.group_id)
            if (j.get("chunk_index") or 0) == ctx.chunk_index
        ]
        if not siblings:
            raise ManualRetryError("not_found")
        latest = max(
            siblings,
            key=lambda j: (j.get("attempt") or 0, j.get("submitted_at") or ""),
        )
        if (latest.get("status") or "") not in ("failed", "cancelled"):
            raise ManualRetryError("not_retryable")
        ctx.rj = RenderJob.from_row(latest)
