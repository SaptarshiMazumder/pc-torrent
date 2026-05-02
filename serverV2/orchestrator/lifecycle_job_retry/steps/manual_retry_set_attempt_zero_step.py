"""ManualRetrySetAttemptZeroStep — manual-retry pipeline.

Sets ``ctx.next_attempt = 0`` so the new job row gets attempt=0,
matching the legacy "fresh attempt with attempt=0" policy of manual
retry.  This grants the chunk a full fresh budget of MAX_RETRIES
auto-retries again on top of whatever the user pressed.

# SOURCE: lifecycle.py:408 (legacy)
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class ManualRetrySetAttemptZeroStep:

    def run(self, ctx: RetryContext) -> None:
        ctx.next_attempt = 0
