"""TryRetryStep — delegates to ``RetryDispatcher.attempt`` and
records the outcome on the context.

In the failure pipeline this is gated by ``ctx.we_own_retry`` so a
losing CAS doesn't trigger a duplicate retry.  In the cancel pipeline
the gating is irrelevant because ``ReleaseLedgerUnconditionalStep``
sets ``we_own_retry = True`` upstream.  The single
``requires_we_own_retry`` flag on the constructor expresses both
modes -- failure passes True, cancel passes False (or omits).
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class TryRetryStep:

    def __init__(self, *, requires_we_own_retry: bool = False) -> None:
        self._requires_we_own_retry = requires_we_own_retry

    def run(self, ctx: TerminationContext) -> None:
        if self._requires_we_own_retry and not ctx.we_own_retry:
            return
        ctx.retried = ctx.deps.retry_dispatcher.attempt(
            job_id=ctx.job_id, raw=ctx.raw, error=ctx.error,
        )
