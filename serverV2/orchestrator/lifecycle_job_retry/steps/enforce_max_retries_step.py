"""EnforceMaxRetriesStep — auto-retry pipeline.

Sets ``ctx.next_attempt = (rj.attempt or 0) + 1`` and aborts (with the
legacy warning log) if the new attempt exceeds the live max_retries.

The max_retries value is read from the live Firestore-backed config on
every step run via ``ctx.deps.get_max_retries()`` -- changes saved in
the desktop ConfigurationPage take effect on the next failure, not at
the next server restart.
"""

from __future__ import annotations

import logging

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
        max_retries = ctx.deps.get_max_retries()
        next_attempt = (rj.attempt or 0) + 1
        if next_attempt > max_retries:
            frame_start, frame_end, _ = ctx.remaining
            log.warning(
                "Job %s: max retries (%d) exhausted for frames %d-%d",
                rj.job_id, max_retries, frame_start, frame_end,
            )
            ctx.aborted = True
            return
        ctx.next_attempt = next_attempt
