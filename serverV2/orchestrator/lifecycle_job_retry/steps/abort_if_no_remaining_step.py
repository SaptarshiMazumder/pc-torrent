"""AbortIfNoRemainingStep — auto-retry pipeline.

Silently sets ``ctx.aborted = True`` when the chunk has no frames left
to render.  Matches the legacy ``return False`` (no log).

# SOURCE: retry_dispatcher.py:96-98 (legacy)
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class AbortIfNoRemainingStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if ctx.remaining is None:
            ctx.aborted = True
