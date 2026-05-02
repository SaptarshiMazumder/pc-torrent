"""ManualRetryRaiseIfActiveSiblingStep — manual-retry pipeline.

Refuses the manual retry when there's already an in-flight sibling for
this chunk.  Prevents double-firing.

# SOURCE: lifecycle.py:363-365 (legacy)
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)
from serverV2.orchestrator.lifecycle_job_retry.manual_retry_error import (
    ManualRetryError,
)


class ManualRetryRaiseIfActiveSiblingStep:

    def run(self, ctx: RetryContext) -> None:
        active = ctx.deps.in_progress_repo.current_job_for(
            ctx.group_id, ctx.chunk_index,
        )
        if active is not None:
            raise ManualRetryError("active_sibling_exists")
