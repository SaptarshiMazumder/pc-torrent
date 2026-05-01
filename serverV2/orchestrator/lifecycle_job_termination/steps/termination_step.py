"""TerminationStep — protocol implemented by every step class."""

from __future__ import annotations

from typing import Protocol

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class TerminationStep(Protocol):
    """One action in a job-termination pipeline.

    Steps are stateless; configuration is fixed at construction.  They
    read and mutate the shared ``TerminationContext`` -- the executor
    iterates the pipeline in order, calling ``run(ctx)`` on each step.

    Pipelines (FAILURE_PIPELINE / CANCEL_PIPELINE) are immutable tuples
    of step instances built once via ``TerminationPipelineBuilder``.
    Building does NOT execute anything; that's ``JobTerminator``'s job.
    """

    def run(self, ctx: TerminationContext) -> None:
        ...
