"""TerminationPipelineBuilder — fluent builder that produces a tuple
of step instances.

The builder is **rules, not action**.  Each method appends a step
descriptor to the internal list; ``build()`` freezes the list into an
immutable tuple and returns it.  Nothing executes during the build --
no DB calls, no Redis hits, no logs.

The two flows in ``pipelines.py`` (FAILURE_PIPELINE and
CANCEL_PIPELINE) are constructed once at import time via this builder
and reused across every termination call.

Each builder method returns ``self`` so calls chain.  Adding new step
configurations to a flow is one builder line.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.steps import (
    CancelProviderStep,
    LogFailureOutcomeStep,
    MarkTerminalStep,
    ReconcileGroupStep,
    ReleaseLedgerAtomicCasStep,
    ReleaseLedgerUnconditionalStep,
    ReleaseMachineStep,
    ReleaseTerminalGroupResourcesStep,
    StopMonitorStep,
    TerminationStep,
    TryRetryStep,
)


class TerminationPipelineBuilder:

    def __init__(self) -> None:
        self._steps: list[TerminationStep] = []

    # ------------------------------------------------------------------
    # ledger
    # ------------------------------------------------------------------

    def release_ledger_atomic_cas(self) -> "TerminationPipelineBuilder":
        self._steps.append(ReleaseLedgerAtomicCasStep())
        return self

    def release_ledger_unconditional(self) -> "TerminationPipelineBuilder":
        self._steps.append(ReleaseLedgerUnconditionalStep())
        return self

    # ------------------------------------------------------------------
    # retry
    # ------------------------------------------------------------------

    def try_retry(
        self, *, requires_we_own_retry: bool = False,
    ) -> "TerminationPipelineBuilder":
        self._steps.append(
            TryRetryStep(requires_we_own_retry=requires_we_own_retry),
        )
        return self

    # ------------------------------------------------------------------
    # status / machine
    # ------------------------------------------------------------------

    def mark_terminal(
        self, status: str, *, error_override: str | None = None,
    ) -> "TerminationPipelineBuilder":
        self._steps.append(
            MarkTerminalStep(status, error_override=error_override),
        )
        return self

    def release_machine_if_community(self) -> "TerminationPipelineBuilder":
        self._steps.append(ReleaseMachineStep())
        return self

    # ------------------------------------------------------------------
    # provider / monitor
    # ------------------------------------------------------------------

    def stop_monitor(self) -> "TerminationPipelineBuilder":
        self._steps.append(StopMonitorStep())
        return self

    def cancel_provider(self) -> "TerminationPipelineBuilder":
        self._steps.append(CancelProviderStep())
        return self

    # ------------------------------------------------------------------
    # logging / housekeeping
    # ------------------------------------------------------------------

    def log_failure_outcome(self) -> "TerminationPipelineBuilder":
        self._steps.append(LogFailureOutcomeStep())
        return self

    def reconcile_group(
        self, *, only_if_not_retried_and_owned: bool = False,
    ) -> "TerminationPipelineBuilder":
        self._steps.append(
            ReconcileGroupStep(
                only_if_not_retried_and_owned=only_if_not_retried_and_owned,
            ),
        )
        return self

    def release_terminal_group_resources(self) -> "TerminationPipelineBuilder":
        self._steps.append(ReleaseTerminalGroupResourcesStep())
        return self

    # ------------------------------------------------------------------
    # finalize
    # ------------------------------------------------------------------

    def build(self) -> tuple[TerminationStep, ...]:
        return tuple(self._steps)
