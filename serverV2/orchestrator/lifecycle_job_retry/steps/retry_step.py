"""RetryStep — protocol implemented by every step in a retry pipeline."""

from __future__ import annotations

from typing import Protocol

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class RetryStep(Protocol):
    """One action in a retry pipeline.

    Steps are stateless; configuration is fixed at construction.  They
    read and mutate the shared ``RetryContext`` -- the executor iterates
    the pipeline in order, calling ``run(ctx)`` on each step.

    Pipelines (AUTO_RETRY_PIPELINE / MANUAL_RETRY_PIPELINE) are immutable
    tuples of step instances built once via ``RetryPipelineBuilder``.
    Building does NOT execute anything; that's ``RetryExecutor``'s job.
    """

    def run(self, ctx: RetryContext) -> None:
        ...
