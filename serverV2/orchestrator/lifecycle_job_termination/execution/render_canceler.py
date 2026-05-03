"""RenderCanceler — group-level multi-pass cancel.

Reuses the per-job step classes from ``steps/`` but orchestrates them
in a multi-pass shape: every job in the group is marked terminal in
the DB BEFORE any monitor stop or provider cancel runs.  Once every
row is terminal, late signals from still-running monitors hit
CallbackRouter's terminal short-circuit and bounce.  Group-scoped
operations (drain queue, release every chunk slot) happen between
passes.

Behavior parity with the previous implementation:

  Pass 1 -- group terminal in render_groups.
  Pass 2 -- for each active job: MarkTerminalStep + ReleaseMachineStep
            (fully terminal in the DB before any monitor / provider
            tear-down begins).
  Pass 3 -- for each active job: StopMonitorStep
  Pass 4 -- group-scoped: drain dispatch_queue + release_all
            in_progress_chunks for the group.
  Pass 5 -- for each active job: CancelProviderStep (slowest; RPC).

The terminal snapshot write at the end of the cancel flow stays on
``RenderLifecycle.cancel_render`` -- it's group-rollup logic, not
cancel logic, and it's reused by ``reconcile_group_status`` for done
/ failed transitions.
"""

from __future__ import annotations

import logging
from typing import Any

from serverV2.fleets.modal.modal_active_jobs_hooks import ModalActiveJobsHooks
from serverV2.orchestrator.lifecycle_job_termination.steps import (
    CancelProviderStep,
    MarkTerminalStep,
    ReleaseMachineStep,
    StopMonitorStep,
)
from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    LifecycleDeps,
    TerminationContext,
)
from serverV2.repositories.dispatch_queue_repository import DispatchQueueRepository
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


_GROUP_CANCEL_ERROR = "Cancelled by user"


class RenderCanceler:

    def __init__(
        self,
        *,
        group_repo: RenderGroupRepository,
        job_repo: JobRepository,
        queue_repo: DispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        deps: LifecycleDeps,
        modal_active_jobs_hooks: ModalActiveJobsHooks,
    ) -> None:
        self._group_repo = group_repo
        self._job_repo = job_repo
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._deps = deps
        self._modal_active_jobs_hooks = modal_active_jobs_hooks
        # Reused step instances -- stateless and configurable.
        self._mark_step = MarkTerminalStep(
            "cancelled", error_override=_GROUP_CANCEL_ERROR,
        )
        self._release_machine_step = ReleaseMachineStep()
        self._stop_monitor_step = StopMonitorStep()
        self._cancel_provider_step = CancelProviderStep()

    def cancel(self, group_id: str) -> dict[str, Any]:
        """Run the multi-pass cancel.  Returns
        ``{'cancelled_jobs': N}`` where N is the number of jobs that
        were active at the start of the call.  Caller (lifecycle
        ``cancel_render``) writes the terminal snapshot afterwards."""
        # Pass 1 -- group terminal.
        self._group_repo.update_status(group_id, "cancelled")
        jobs = self._job_repo.get_active_by_group(group_id)

        # Pass 2 -- every job terminal in the DB BEFORE any monitor
        # stop / provider cancel runs.  Failure signals fired during
        # passes 3-5 will hit terminal rows at CallbackRouter and be
        # dropped.
        contexts = [
            TerminationContext(
                job_id=job["id"],
                raw=job,
                error=_GROUP_CANCEL_ERROR,
                deps=self._deps,
            )
            for job in jobs
        ]
        # Modal-side bookkeeping for any modal_serverless jobs in the
        # group.  Group cancel doesn't route through the success/failure
        # callbacks, so the hook fires here.  SREM-backed and idempotent.
        for ctx in contexts:
            if (ctx.raw.get("machine_type") or "") == "modal_serverless":
                gpu_type = (ctx.raw.get("gpu_type") or "").strip()
                if gpu_type:
                    self._modal_active_jobs_hooks.on_terminal(
                        job_id=ctx.job_id, gpu_type=gpu_type,
                    )
        for ctx in contexts:
            self._mark_step.run(ctx)
            self._release_machine_step.run(ctx)

        # Pass 3 -- stop monitor threads.
        for ctx in contexts:
            self._stop_monitor_step.run(ctx)

        # Pass 4 -- drain queue + release every chunk slot for the
        # group.  Group-scoped operations; no per-job iteration.
        drained = self._queue_repo.drain(group_id)
        if drained:
            log.info("Group %s: drained %d items from dispatch queue", group_id, drained)
        self._in_progress.release_all(group_id)

        # Pass 5 -- provider-side cancellation (slowest; may RPC out).
        for ctx in contexts:
            self._cancel_provider_step.run(ctx)

        log.info("Group %s: cancelled %d jobs", group_id, len(jobs))
        return {"cancelled_jobs": len(jobs)}
