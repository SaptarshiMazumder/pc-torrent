"""RenderCanceler — tear down a render group via the multi-pass dance.

Single responsibility: orchestrate the per-job teardown across N jobs
in the order that preserves correctness when many jobs are involved.

The order matters and behaviorally must match what
``RenderLifecycle.cancel_render`` did before this extraction:

  Pass 1: mark group cancelled
  Pass 2: for each active job -- mark_cancelled (DB row terminal,
          machine released).  Done for ALL jobs before any monitor
          stop / provider cancel runs, so any failure signal that
          fires from a still-running monitor during the rest of the
          method bounces off CallbackRouter's terminal-guard.
  Pass 3: for each active job -- stop_monitoring.
  Pass 4: drain dispatch_queue + release_all in_progress_chunks for
          the group (group-scoped, no per-job iteration needed).
  Pass 5: for each active job -- cancel_provider (slowest).

If we fused these into one per-job loop, Job A's provider call would
happen before Job B's status flip, and Job B's monitor could fire a
failure signal that lands in handle_chunk_failed (still non-terminal),
which would overwrite Job B's status to 'failed' before Pass 2 ever
got to it.  Multi-pass avoids that.

The terminal snapshot write at the end of the cancel flow stays on
``RenderLifecycle.cancel_render`` -- it's group-rollup logic, not
cancel logic, and it's reused by ``reconcile_group_status`` for done
/ failed transitions.
"""

from __future__ import annotations

import logging
from typing import Any

from serverV2.orchestrator.lifecycle_cancel.job_canceler import JobCanceler
from serverV2.repositories.dispatch_queue_repository import DispatchQueueRepository
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


class RenderCanceler:

    def __init__(
        self,
        *,
        group_repo: RenderGroupRepository,
        job_repo: JobRepository,
        queue_repo: DispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        job_canceler: JobCanceler,
    ) -> None:
        self._group_repo = group_repo
        self._job_repo = job_repo
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._job_canceler = job_canceler

    def cancel(self, group_id: str) -> dict[str, Any]:
        """Run the multi-pass cancel.  Returns ``{'cancelled_jobs': N}``
        where N is the number of jobs that were active at the start of
        the call.  Caller (``RenderLifecycle.cancel_render``) writes
        the terminal snapshot afterwards."""
        # Pass 1 -- group terminal.
        self._group_repo.update_status(group_id, "cancelled")
        jobs = self._job_repo.get_active_by_group(group_id)

        # Pass 2 -- every job terminal in the DB BEFORE any monitor
        # stop / provider cancel runs.  Any failure signal that fires
        # while passes 3-5 execute will hit a terminal row at
        # CallbackRouter and be dropped.
        for job in jobs:
            self._job_canceler.mark_cancelled(job)

        # Pass 3 -- stop monitor threads.
        for job in jobs:
            self._job_canceler.stop_monitoring(job)

        # Pass 4 -- drain queue + release every chunk slot for the
        # group.  Group-scoped operations; no per-job iteration.
        drained = self._queue_repo.drain(group_id)
        if drained:
            log.info("Group %s: drained %d items from dispatch queue", group_id, drained)
        self._in_progress.release_all(group_id)

        # Pass 5 -- provider-side cancellation (slowest, may RPC out).
        for job in jobs:
            self._job_canceler.cancel_provider(job)

        log.info("Group %s: cancelled %d jobs", group_id, len(jobs))
        return {"cancelled_jobs": len(jobs)}
