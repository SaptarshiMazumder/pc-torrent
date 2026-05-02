"""EnforceMaxRetriesStep — auto-retry pipeline.

Sets ``ctx.next_attempt = (rj.attempt or 0) + 1`` and aborts (with the
legacy warning log) if the new attempt exceeds ``MAX_RETRIES``.

# SOURCE: retry_dispatcher.py:100-106 (legacy)
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.config import MAX_RETRIES
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class EnforceMaxRetriesStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        rj = ctx.rj
        if rj is None or ctx.remaining is None:
            return
        next_attempt = (rj.attempt or 0) + 1
        if next_attempt > MAX_RETRIES:
            frame_start, frame_end, _ = ctx.remaining
            log.warning(
                "Job %s: max retries (%d) exhausted for frames %d-%d",
                rj.job_id, MAX_RETRIES, frame_start, frame_end,
            )
            ctx.aborted = True
            return
        ctx.next_attempt = next_attempt
