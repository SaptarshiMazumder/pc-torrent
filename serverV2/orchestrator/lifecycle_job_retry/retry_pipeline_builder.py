"""RetryPipelineBuilder — fluent builder that produces a tuple of step
instances for a retry pipeline.

The builder is **rules, not action**.  Each method appends a step
descriptor to the internal list; ``build()`` freezes the list into an
immutable tuple and returns it.  Nothing executes during the build --
no DB calls, no logs.

The two flows in ``pipelines.py`` (AUTO_RETRY_PIPELINE and
MANUAL_RETRY_PIPELINE) are constructed once at import time via this
builder and reused across every retry call.

Each builder method returns ``self`` so calls chain.  Adding new step
configurations to a flow is one builder line.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.steps import (
    AbortIfGroupTerminalStep,
    AbortIfNoRemainingStep,
    BuildRetryChunkRequestStep,
    ComputeRemainingFramesStep,
    EnforceMaxRetriesStep,
    LoadDispatchContextStep,
    LoadJobAndGroupStep,
    LogAutoRetryStep,
    LogManualRetryStep,
    ManualRetryBuildResultStep,
    ManualRetryFlipGroupPendingStep,
    ManualRetryLoadGroupStep,
    ManualRetryRaiseIfActiveSiblingStep,
    ManualRetryRaiseIfNoRemainingStep,
    ManualRetryResolveLatestSiblingStep,
    ManualRetrySetAttemptZeroStep,
    ParkRetryToPendingStep,
    RetryStep,
)


class RetryPipelineBuilder:

    def __init__(self) -> None:
        self._steps: list[RetryStep] = []

    # ------------------------------------------------------------------
    # auto-retry-specific steps (silent abort + log)
    # ------------------------------------------------------------------

    def load_job_and_group(self) -> "RetryPipelineBuilder":
        self._steps.append(LoadJobAndGroupStep())
        return self

    def abort_if_group_terminal(self) -> "RetryPipelineBuilder":
        self._steps.append(AbortIfGroupTerminalStep())
        return self

    def abort_if_no_remaining(self) -> "RetryPipelineBuilder":
        self._steps.append(AbortIfNoRemainingStep())
        return self

    def enforce_max_retries(self) -> "RetryPipelineBuilder":
        self._steps.append(EnforceMaxRetriesStep())
        return self

    def log_auto_retry(self) -> "RetryPipelineBuilder":
        self._steps.append(LogAutoRetryStep())
        return self

    # ------------------------------------------------------------------
    # manual-retry-specific steps (raise ManualRetryError)
    # ------------------------------------------------------------------

    def manual_retry_load_group(self) -> "RetryPipelineBuilder":
        self._steps.append(ManualRetryLoadGroupStep())
        return self

    def manual_retry_raise_if_active_sibling(self) -> "RetryPipelineBuilder":
        self._steps.append(ManualRetryRaiseIfActiveSiblingStep())
        return self

    def manual_retry_resolve_latest_sibling(self) -> "RetryPipelineBuilder":
        self._steps.append(ManualRetryResolveLatestSiblingStep())
        return self

    def manual_retry_raise_if_no_remaining(self) -> "RetryPipelineBuilder":
        self._steps.append(ManualRetryRaiseIfNoRemainingStep())
        return self

    def manual_retry_set_attempt_zero(self) -> "RetryPipelineBuilder":
        self._steps.append(ManualRetrySetAttemptZeroStep())
        return self

    def log_manual_retry(self) -> "RetryPipelineBuilder":
        self._steps.append(LogManualRetryStep())
        return self

    def manual_retry_flip_group_pending(self) -> "RetryPipelineBuilder":
        self._steps.append(ManualRetryFlipGroupPendingStep())
        return self

    def manual_retry_build_result(self) -> "RetryPipelineBuilder":
        self._steps.append(ManualRetryBuildResultStep())
        return self

    # ------------------------------------------------------------------
    # shared steps (used by both flows)
    # ------------------------------------------------------------------

    def compute_remaining_frames(self) -> "RetryPipelineBuilder":
        self._steps.append(ComputeRemainingFramesStep())
        return self

    def load_dispatch_context(self) -> "RetryPipelineBuilder":
        self._steps.append(LoadDispatchContextStep())
        return self

    def build_retry_chunk_request(self) -> "RetryPipelineBuilder":
        self._steps.append(BuildRetryChunkRequestStep())
        return self

    def park_retry_to_pending(self) -> "RetryPipelineBuilder":
        self._steps.append(ParkRetryToPendingStep())
        return self

    # ------------------------------------------------------------------
    # finalize
    # ------------------------------------------------------------------

    def build(self) -> tuple[RetryStep, ...]:
        return tuple(self._steps)
