"""JobTerminator — the executor.

Runs a pre-built termination pipeline (immutable tuple of step
instances) against a context.  This is the only place that calls
``step.run(ctx)`` -- the builder produces rules, the executor applies
them.

The executor is stateless across calls; all per-call state lives on
the ``TerminationContext`` it constructs.  Steps' interactions with
each other happen only through context fields.

Lifecycle's ``handle_chunk_failed`` and ``cancel_one_job`` are now
~5-line wrappers: fetch the job row, decide whether to bail (already
terminal / not found), construct the context, and call ``execute``
with the right pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from serverV2.orchestrator.anti_affinity import AntiAffinityExclusions
from serverV2.orchestrator.lifecycle_job_termination.steps import TerminationStep
from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    LifecycleDeps,
    TerminationContext,
)

log = logging.getLogger(__name__)


class JobTerminator:

    def __init__(self, *, deps: LifecycleDeps) -> None:
        self._deps = deps

    def execute(
        self,
        *,
        pipeline: tuple[TerminationStep, ...],
        job_id: str,
        raw: dict[str, Any],
        error: str,
        exclusions: AntiAffinityExclusions,
    ) -> TerminationContext:
        ctx = TerminationContext(
            job_id=job_id,
            raw=raw,
            error=error,
            deps=self._deps,
            exclusions=exclusions,
        )
        log.info(
            "[RETRY_DEBUG] JobTerminator.execute(%s): pipeline has %d steps", job_id, len(pipeline),
        )
        for step in pipeline:
            step_name = type(step).__name__
            log.info("[RETRY_DEBUG] JobTerminator(%s): -> step %s", job_id, step_name)
            try:
                step.run(ctx)
            except Exception as exc:
                log.error(
                    "[RETRY_DEBUG] JobTerminator(%s): step %s RAISED: %r",
                    job_id, step_name, exc,
                )
                raise
            log.info(
                "[RETRY_DEBUG] JobTerminator(%s): <- step %s done (we_own_retry=%s, retried=%s)",
                job_id, step_name, ctx.we_own_retry, ctx.retried,
            )
        return ctx
