"""Job-retry package.

Houses the auto-retry and manual-retry pipelines and everything they
need: the granular steps (``steps/``), the rules-style pipeline builder
(``retry_pipeline_builder``), the two pre-built pipelines
(``pipelines``), the executor that runs a pipeline against a context
(``execution/retry_executor``), and the typed manual-retry refusal
exception (``manual_retry_error``).

Design: builder produces immutable rules (tuples of step instances);
the executor is the only thing that calls ``step.run(ctx)``.  Building
does NOT execute anything.

Anti-affinity is resolved by the caller (``RenderLifecycle``) BEFORE
invoking the pipeline and is passed in via the context — the pipeline
itself never queries job history for exclusions.
"""

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
    RetryDeps,
)
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_executor import (
    RetryExecutor,
)
from serverV2.orchestrator.lifecycle_job_retry.manual_retry_error import (
    RETRY_REASON_HTTP_STATUS,
    ManualRetryError,
)
from serverV2.orchestrator.lifecycle_job_retry.pipelines import (
    AUTO_RETRY_PIPELINE,
    MANUAL_RETRY_PIPELINE,
)
from serverV2.orchestrator.lifecycle_job_retry.retry_pipeline_builder import (
    RetryPipelineBuilder,
)

__all__ = [
    "AUTO_RETRY_PIPELINE",
    "MANUAL_RETRY_PIPELINE",
    "ManualRetryError",
    "RETRY_REASON_HTTP_STATUS",
    "RetryContext",
    "RetryDeps",
    "RetryExecutor",
    "RetryPipelineBuilder",
]
