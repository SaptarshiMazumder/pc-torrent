"""LogAutoRetryStep — auto-retry pipeline.

Emits the legacy "Job X: requeued frames A-B (attempt N/M) on TARGET"
info log.  Lives as its own step so the log content stays close to the
auto-retry policy and doesn't have to be parameterized into a shared
dispatch step.

# SOURCE: retry_dispatcher.py:143-151 (legacy)
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.config import MAX_RETRIES
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class LogAutoRetryStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if ctx.retry_task is None or ctx.rj is None or ctx.chunk_request is None:
            return
        retry_task = ctx.retry_task
        target_label = (
            f"{retry_task.fleet}/{retry_task.gpu_type}"
            if retry_task.gpu_type
            else f"{retry_task.fleet}/{retry_task.machine_id}"
        )
        log.info(
            "Job %s: requeued frames %d-%d (attempt %d/%d) on %s",
            ctx.rj.job_id,
            ctx.chunk_request.frame_start,
            ctx.chunk_request.frame_end,
            ctx.next_attempt,
            MAX_RETRIES,
            target_label,
        )
