"""MarkForceRetryStep — flips the manual-retry bypass flag on context.

One responsibility: set ``ctx.force_retry = True`` so that the
downstream ``EnqueueRetryDispatchStep`` stamps the flag onto the
``DispatchContext`` it builds.  The dispatch handler reads that flag
to bypass its terminal-group guard when the parent group is marked
``failed``.

Only the manual-retry pipeline includes this step; auto-retry never
runs against terminal groups (``AbortIfGroupTerminalStep`` short-
circuits earlier), so the flag stays at its default False there.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class MarkForceRetryStep:

    def run(self, ctx: RetryContext) -> None:
        ctx.force_retry = True
