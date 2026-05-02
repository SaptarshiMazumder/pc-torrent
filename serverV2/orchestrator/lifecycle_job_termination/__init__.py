"""Job-termination package.

Houses everything related to terminating one or more render jobs:
the per-job teardown steps (``steps/``), the rules-style pipeline
builder (``termination_pipeline_builder``), the two pre-built
pipelines (``pipelines``) for the failure and per-job-cancel flows,
the executor that runs a pipeline against a context
(``job_terminator``), and the group-level multi-pass cancel
(``render_canceler``).

Auto-retry inside the failure / cancel pipelines is dispatched by
``TryRetryStep`` which delegates to the ``lifecycle_job_retry``
package (sibling to this one).  No retry logic lives here.

Design: builder produces immutable rules (tuples of step instances);
the executor is the only thing that calls ``step.run(ctx)``.  Building
does NOT execute anything.
"""

from serverV2.orchestrator.lifecycle_job_termination.execution.job_terminator import JobTerminator
from serverV2.orchestrator.lifecycle_job_termination.pipelines import (
    CANCEL_PIPELINE,
    FAILURE_PIPELINE,
)
from serverV2.orchestrator.lifecycle_job_termination.execution.render_canceler import RenderCanceler
from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    LifecycleDeps,
    TerminationContext,
)
from serverV2.orchestrator.lifecycle_job_termination.termination_pipeline_builder import (
    TerminationPipelineBuilder,
)

__all__ = [
    "CANCEL_PIPELINE",
    "FAILURE_PIPELINE",
    "JobTerminator",
    "LifecycleDeps",
    "RenderCanceler",
    "TerminationContext",
    "TerminationPipelineBuilder",
]
