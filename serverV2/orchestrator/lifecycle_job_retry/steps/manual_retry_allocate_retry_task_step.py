"""ManualRetryAllocateRetryTaskStep — manual-retry pipeline.

Manual-retry counterpart to ``AllocateRetryTaskStep``.  Same allocation
logic; raises ``no_eligible_target`` instead of logging + aborting.

# SOURCE: lifecycle.py:414-419 (legacy)
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)
from serverV2.orchestrator.lifecycle_job_retry.manual_retry_error import (
    ManualRetryError,
)


class ManualRetryAllocateRetryTaskStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.chunk_request is None:
            raise ManualRetryError("no_eligible_target")
        retry_task = ctx.deps.allocation_client.plan_retry(
            tier=ctx.tier,
            chunk_request=ctx.chunk_request,
        )
        if retry_task is None:
            raise ManualRetryError("no_eligible_target")
        ctx.retry_task = retry_task
