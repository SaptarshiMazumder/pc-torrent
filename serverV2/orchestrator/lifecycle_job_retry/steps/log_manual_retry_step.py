"""LogManualRetryStep — manual-retry pipeline.

Emits the legacy "Manual retry for chunk X (group Y): frames A-B on TARGET
(attempt reset to 0, latest failed attempt was N)" info log.  The
"latest failed attempt was N" detail differentiates this from the
auto-retry log line.

# SOURCE: lifecycle.py:421-431 (legacy)
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class LogManualRetryStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.retry_task is None or ctx.rj is None or ctx.chunk_request is None:
            return
        retry_task = ctx.retry_task
        target_label = (
            f"{retry_task.fleet}/{retry_task.gpu_type}"
            if retry_task.gpu_type
            else f"{retry_task.fleet}/{retry_task.machine_id}"
        )
        log.info(
            "Manual retry for chunk %d (group %s): frames %d-%d on %s "
            "(attempt reset to 0, latest failed attempt was %d)",
            ctx.chunk_index,
            ctx.group_id,
            ctx.chunk_request.frame_start,
            ctx.chunk_request.frame_end,
            target_label,
            ctx.rj.attempt or 0,
        )
