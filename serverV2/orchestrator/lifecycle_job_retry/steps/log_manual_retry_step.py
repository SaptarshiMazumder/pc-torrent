"""LogManualRetryStep -- manual-retry pipeline.

Logs that a user-triggered retry was parked.  No target is chosen
synchronously anymore -- the daemon picks one on its next tick.
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class LogManualRetryStep:

    def run(self, ctx: RetryContext) -> None:
        if not ctx.parked or ctx.rj is None or ctx.chunk_request is None:
            return
        log.info(
            "Manual retry parked for chunk %d (group %s): frames %d-%d "
            "(attempt reset to 0, latest failed attempt was %d) -- "
            "daemon will plan a target on next tick",
            ctx.chunk_index,
            ctx.group_id,
            ctx.chunk_request.frame_start,
            ctx.chunk_request.frame_end,
            ctx.rj.attempt or 0,
        )
