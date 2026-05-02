"""AllocateRetryTaskStep — auto-retry pipeline.

Picks the strategy, calls ``allocate_retry``, and aborts (with the
legacy error log) if no eligible target is found.  Manual retry uses
``ManualRetryAllocateRetryTaskStep`` instead — same logic, but raises
``ManualRetryError("no_eligible_target")`` on the same condition.

# SOURCE: retry_dispatcher.py:130-141 (legacy)
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.allocation import tiers
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class AllocateRetryTaskStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if ctx.chunk_request is None or ctx.rj is None:
            return
        retry_strategy = ctx.deps.strategy_picker(
            tiers.normalize(ctx.tier),
            ctx.file_size_bytes if ctx.file_size_bytes is not None else 0,
            ctx.chunk_request.total_frames,
        )
        retry_task = retry_strategy.allocate_retry(
            ctx.chunk_request, ctx.deps.resource_picker(),
        )
        if retry_task is None:
            log.error(
                "Job %s: no eligible target for retry of frames %d-%d (group %s)",
                ctx.rj.job_id,
                ctx.chunk_request.frame_start,
                ctx.chunk_request.frame_end,
                ctx.group_id,
            )
            ctx.aborted = True
            return
        ctx.retry_task = retry_task
