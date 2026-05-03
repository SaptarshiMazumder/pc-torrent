"""ModalActiveJobsHooks — Modal-side bookkeeping invoked when Modal
jobs cross lifecycle boundaries.

Today these methods delegate to ``ModalActiveJobsTracker``.  Future
Modal-specific cleanup (telemetry counters, secondary caches, fleet-
specific logging) extends this class instead of scattering across
call sites.

Call sites are deliberately few:

* ``on_dispatch`` — fired once by ``ModalFleetStrategy.dispatch`` after
  the new job row is inserted.
* ``on_terminal`` — fired by lifecycle handlers when a Modal job
  transitions to terminal: ``handle_chunk_succeeded``,
  ``handle_chunk_failed``, ``cancel_one_job``, and
  ``RenderCanceler.cancel`` (group cancel).  Per the project's
  "rely on the callback chain" decision, the Modal monitor itself
  doesn't fire this -- it routes through the success/failure
  callbacks instead.

Idempotency at the tracker level (SREM is a no-op if the member is
already gone) means race-condition double-fires across these sites
don't break the count.
"""

from __future__ import annotations

from serverV2.fleets.modal.modal_active_jobs_tracker import (
    ModalActiveJobsTracker,
)


class ModalActiveJobsHooks:

    def __init__(self, *, tracker: ModalActiveJobsTracker) -> None:
        self._tracker = tracker

    def on_dispatch(self, *, job_id: str, gpu_type: str) -> None:
        self._tracker.add(job_id, gpu_type)

    def on_terminal(self, *, job_id: str, gpu_type: str) -> None:
        self._tracker.remove(job_id, gpu_type)
