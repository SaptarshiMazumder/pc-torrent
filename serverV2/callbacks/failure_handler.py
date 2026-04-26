"""FailureHandler — decides whether to retry or permanently fail a job.

Asks the orchestrator to retry first.  Only marks the job as failed when
retries are exhausted, then asks the orchestrator to roll the change up
to the group.  After Phase 2, also drains the failed fleet's queue —
a slot just opened up regardless of whether the failure was retried
elsewhere.

Job-level mutations (mark_failed, fleet drain) live here.  Group-level
state changes go through the orchestrator — this handler never touches
``render_groups`` directly.

Called ONLY from CallbackRouter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from serverV2.repositories.job_repository import JobRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.dispatch.coordinator import DispatchCoordinator
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class FailureHandler:

    def __init__(self, job_repo: JobRepository) -> None:
        self._job_repo = job_repo
        self._orchestrator: RenderOrchestrator | None = None
        self._coordinator: DispatchCoordinator | None = None

    def set_orchestrator(self, orchestrator: "RenderOrchestrator") -> None:
        self._orchestrator = orchestrator

    def set_coordinator(self, coordinator: "DispatchCoordinator") -> None:
        """Late-bound to break the circular wiring with the coordinator."""
        self._coordinator = coordinator

    def handle(self, job_id: str, error: str) -> None:
        # Capture the failed fleet BEFORE retry — the orchestrator may
        # dispatch the retry on a different fleet (anti-affinity), but the
        # SLOT we just freed is in this job's fleet.
        raw = self._job_repo.get_raw_by_id(job_id)
        failed_fleet = (raw.get("machine_type") or "") if raw else ""

        # Ask orchestrator to retry BEFORE marking failed — ensures a new
        # active job exists so the group never prematurely goes to "failed".
        retried = False
        if self._orchestrator:
            retried = self._orchestrator.on_job_failed(job_id, error)

        self._job_repo.mark_failed(job_id, error)

        if retried:
            log.info("Job %s failed but retry dispatched: %s", job_id, error)
        else:
            log.warning("Job %s permanently failed (retries exhausted): %s", job_id, error)
            job = self._job_repo.get_raw_by_id(job_id)
            group_id = job.get("group_id") if job else None
            if group_id and self._orchestrator is not None:
                self._orchestrator.on_job_failed_terminal(group_id)

        # Slot just opened up in the failed fleet — drain its queue.
        if failed_fleet and self._coordinator is not None:
            try:
                self._coordinator.drain_for_fleet(failed_fleet)
            except Exception as exc:
                log.warning("drain_for_fleet(%s) failed: %s", failed_fleet, exc)
