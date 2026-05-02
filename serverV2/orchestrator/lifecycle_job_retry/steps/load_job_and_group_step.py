"""LoadJobAndGroupStep — auto-retry pipeline first step.

Populates ``ctx.rj``, ``ctx.group_id``, ``ctx.chunk_index``, ``ctx.grp``
from the input row.  Mirror of the opening lines of
``RetryDispatcher.attempt`` (pre-refactor).

Manual retry uses ``ManualRetryLoadGroupStep`` instead — its row
resolution is different (latest sibling lookup, not direct from
``ctx.raw``).

# SOURCE: retry_dispatcher.py:80-90 (legacy)
"""

from __future__ import annotations

from serverV2.core.models import RenderJob
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class LoadJobAndGroupStep:

    def run(self, ctx: RetryContext) -> None:
        rj = RenderJob.from_row(ctx.raw)
        ctx.rj = rj
        ctx.group_id = rj.group_id
        ctx.chunk_index = rj.chunk_index or 0
        ctx.grp = ctx.deps.group_repo.get_by_id(ctx.group_id)
