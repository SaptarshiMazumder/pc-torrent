"""ManualRetryReconcileGroupStep — manual-retry pipeline.

After a manual retry's new pending job appears, the group aggregator
sees it and unlocks the group from any terminal state (failed) back to
running.  Auto retry doesn't run reconcile here (the failure pipeline's
reconcile_group step covers it).

# SOURCE: lifecycle.py:448-450 (legacy)
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class ManualRetryReconcileGroupStep:

    def run(self, ctx: RetryContext) -> None:
        ctx.deps.reconcile_group(ctx.group_id)
