"""ManualRetryRaiseIfNoRemainingStep — manual-retry pipeline.

Manual-retry counterpart to ``AbortIfNoRemainingStep``.  Raises the
typed error instead of silently aborting.

# SOURCE: lifecycle.py:387-391 (legacy)
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)
from serverV2.orchestrator.lifecycle_job_retry.manual_retry_error import (
    ManualRetryError,
)


class ManualRetryRaiseIfNoRemainingStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.remaining is None:
            raise ManualRetryError("no_remaining_frames")
