"""RetryExecutor — runs a pre-built retry pipeline against a context.

Mirrors the shape of ``JobTerminator`` in the sibling termination
package: stateless across calls, all per-call state lives on the
``RetryContext`` it constructs.  Builder produces rules (immutable
tuple); executor applies them.

Auto-retry callers ignore the returned context's ``dispatched`` flag
(they care only about whether a retry was enqueued).  Manual-retry
callers read ``ctx.result`` for the API payload.
"""

from __future__ import annotations

from typing import Any

from serverV2.orchestrator.anti_affinity import AntiAffinityExclusions
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
    RetryDeps,
)
from serverV2.orchestrator.lifecycle_job_retry.steps.retry_step import RetryStep


class RetryExecutor:

    def __init__(self, *, deps: RetryDeps) -> None:
        self._deps = deps

    def execute(
        self,
        *,
        pipeline: tuple[RetryStep, ...],
        raw: dict[str, Any],
        exclusions: AntiAffinityExclusions,
        error: str = "",
    ) -> RetryContext:
        ctx = RetryContext(
            raw=raw,
            error=error,
            exclusions=exclusions,
            deps=self._deps,
        )
        for step in pipeline:
            step.run(ctx)
        return ctx
