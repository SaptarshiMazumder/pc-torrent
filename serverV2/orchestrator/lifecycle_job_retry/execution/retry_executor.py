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

import logging
from typing import Any

from serverV2.orchestrator.anti_affinity import AntiAffinityExclusions
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
    RetryDeps,
)
from serverV2.orchestrator.lifecycle_job_retry.steps.retry_step import RetryStep

log = logging.getLogger(__name__)


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
        job_id_for_log = raw.get("id") or "<unknown>"
        log.info(
            "[RETRY_DEBUG] RetryExecutor.execute(%s): pipeline has %d steps",
            job_id_for_log, len(pipeline),
        )
        for step in pipeline:
            step_name = type(step).__name__
            if ctx.aborted:
                log.info(
                    "[RETRY_DEBUG] RetryExecutor(%s): -> step %s SKIPPED (ctx.aborted=True)",
                    job_id_for_log, step_name,
                )
                continue
            log.info("[RETRY_DEBUG] RetryExecutor(%s): -> step %s", job_id_for_log, step_name)
            try:
                step.run(ctx)
            except Exception as exc:
                log.error(
                    "[RETRY_DEBUG] RetryExecutor(%s): step %s RAISED: %r",
                    job_id_for_log, step_name, exc,
                )
                raise
            log.info(
                "[RETRY_DEBUG] RetryExecutor(%s): <- step %s done (aborted=%s, retry_task_set=%s, dispatched=%s)",
                job_id_for_log, step_name, ctx.aborted,
                ctx.retry_task is not None, ctx.dispatched,
            )
        log.info(
            "[RETRY_DEBUG] RetryExecutor(%s): finished (aborted=%s, dispatched=%s)",
            job_id_for_log, ctx.aborted, ctx.dispatched,
        )
        return ctx
