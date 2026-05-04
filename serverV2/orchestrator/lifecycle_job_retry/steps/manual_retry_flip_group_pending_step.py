"""ManualRetryFlipGroupPendingStep — manual-retry pipeline.

When the parent group is in a terminal state (``failed``), flip it to
``pending`` BEFORE the dispatch row is enqueued.  The dispatch handler
treats anything-not-in-(pending,running) as terminal and drops the
row, so this is what unblocks dispatch on a failed group.

If the group is already active (``pending`` / ``running``) — i.e. the
user clicked Retry on a stuck chunk inside an otherwise-rendering
render — leave the status alone.

If the new dispatch + any downstream auto-retries all fail, the
existing FAILURE_PIPELINE reconcile path will flip the group back to
``failed`` naturally.  Manual retry's responsibility ends at unlocking;
re-failure is the system's normal flow, not this step's concern.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


_TERMINAL_GROUP = frozenset({"failed", "cancelled"})


class ManualRetryFlipGroupPendingStep:

    def run(self, ctx: RetryContext) -> None:
        if not ctx.grp:
            return
        current = ctx.grp.get("status") or ""
        if current not in _TERMINAL_GROUP:
            return
        ctx.deps.group_repo.update_status(ctx.group_id, "pending")
