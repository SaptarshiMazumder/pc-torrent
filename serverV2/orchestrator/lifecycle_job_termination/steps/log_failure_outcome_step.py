"""LogFailureOutcomeStep — emits one of three log lines that the old
``handle_chunk_failed`` produced inline.

Branches on the context flags:

* ``not we_own_retry``  -> "superseded" (info)   [lost the CAS]
* ``retried``            -> "retry dispatched" (info)
* otherwise              -> "permanently failed" (warning)

Failure pipeline only.  Cancel pipeline doesn't log -- preserving
existing behavior for both flows.
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)

log = logging.getLogger(__name__)


class LogFailureOutcomeStep:

    def run(self, ctx: TerminationContext) -> None:
        if not ctx.we_own_retry:
            log.info(
                "handle_chunk_failed: signal for job %s superseded "
                "(chunk %s ledger no longer points at this job)",
                ctx.job_id, ctx.chunk_index,
            )
        elif ctx.retried:
            log.info(
                "Job %s failed but retry dispatched: %s",
                ctx.job_id, ctx.error,
            )
        else:
            log.warning(
                "Job %s permanently failed (no retry dispatched): %s",
                ctx.job_id, ctx.error,
            )
