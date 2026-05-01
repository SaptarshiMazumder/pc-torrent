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

from typing import Any

from serverV2.orchestrator.lifecycle_job_termination.steps import TerminationStep
from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    LifecycleDeps,
    TerminationContext,
)


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
    ) -> TerminationContext:
        ctx = TerminationContext(
            job_id=job_id,
            raw=raw,
            error=error,
            deps=self._deps,
        )
        for step in pipeline:
            step.run(ctx)
        return ctx
