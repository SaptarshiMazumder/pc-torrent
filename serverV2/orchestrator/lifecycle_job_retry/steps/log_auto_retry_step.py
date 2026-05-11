"""LogAutoRetryStep -- auto-retry pipeline.

Logs that the retry was parked for re-evaluation by the dispatch
daemon.  No target is chosen synchronously anymore -- the daemon
picks one when it processes the parked row on a future tick, so this
log has no fleet/gpu_type info to print.
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class LogAutoRetryStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if not ctx.parked or ctx.rj is None or ctx.chunk_request is None:
            return
        log.info(
            "Job %s: parked retry for frames %d-%d (attempt %d/%d) -- "
            "daemon will plan a target on next tick",
            ctx.rj.job_id,
            ctx.chunk_request.frame_start,
            ctx.chunk_request.frame_end,
            ctx.next_attempt,
            ctx.deps.get_max_retries(),
        )
