"""TryRetryStep — delegates to ``RetryExecutor`` running the
``AUTO_RETRY_PIPELINE`` and records the outcome on the context.

In the failure pipeline this is gated by ``ctx.we_own_retry`` so a
losing CAS doesn't trigger a duplicate retry.  In the cancel pipeline
the gating is irrelevant because ``ReleaseLedgerUnconditionalStep``
sets ``we_own_retry = True`` upstream.  The single
``requires_we_own_retry`` flag on the constructor expresses both
modes -- failure passes True, cancel passes False (or omits).

Anti-affinity exclusions are pre-resolved on ``ctx.exclusions`` by
``RenderLifecycle`` before the termination pipeline runs.  We just
forward them to the retry pipeline.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry import AUTO_RETRY_PIPELINE
from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class TryRetryStep:

    def __init__(self, *, requires_we_own_retry: bool = False) -> None:
        self._requires_we_own_retry = requires_we_own_retry

    def run(self, ctx: TerminationContext) -> None:
        if self._requires_we_own_retry and not ctx.we_own_retry:
            return
        retry_ctx = ctx.deps.retry_executor.execute(
            pipeline=AUTO_RETRY_PIPELINE,
            raw=ctx.raw,
            exclusions=ctx.exclusions,
            error=ctx.error,
        )
        ctx.retried = retry_ctx.dispatched
