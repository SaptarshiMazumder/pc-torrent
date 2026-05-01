"""MarkTerminalStep — flips the job row's status to ``cancelled`` or
``failed``.

This is the most important step for both pipelines: once the row's
status is in CallbackRouter's ``_TERMINAL_GROUP_STATUSES``, every
subsequent failure / success / heartbeat signal for this job hits the
terminal-guard short-circuit and is silently dropped.

``error_override`` lets the cancel pipeline hard-code "Cancelled by
user" instead of using ``ctx.error`` -- failure uses ``ctx.error``,
cancel ignores it.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class MarkTerminalStep:

    def __init__(self, status: str, *, error_override: str | None = None) -> None:
        if status not in ("failed", "cancelled"):
            raise ValueError(
                f"MarkTerminalStep status must be 'failed' or 'cancelled', got {status!r}"
            )
        self._status = status
        self._error_override = error_override

    def run(self, ctx: TerminationContext) -> None:
        error = self._error_override if self._error_override is not None else ctx.error
        if self._status == "failed":
            ctx.deps.job_repo.mark_failed(ctx.job_id, error)
        else:
            ctx.deps.job_repo.update_status(ctx.job_id, "cancelled", error=error)
