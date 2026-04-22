"""FailureHandler — decides whether to retry or permanently fail a job.

Asks the orchestrator to retry first.  Only marks the job as failed when
retries are exhausted, then reconciles the group status.
Called ONLY from CallbackRouter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from serverV2.callbacks.group_status_aggregator import compute_group_status
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class FailureHandler:

    def __init__(self, job_repo: JobRepository, group_repo: RenderGroupRepository) -> None:
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._orchestrator: RenderOrchestrator | None = None

    def set_orchestrator(self, orchestrator: RenderOrchestrator) -> None:
        self._orchestrator = orchestrator

    def handle(self, job_id: str, error: str) -> None:
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
            if job and job.get("group_id"):
                self._reconcile_group(job["group_id"])

    def _reconcile_group(self, group_id: str) -> None:
        group = self._group_repo.get_by_id(group_id)
        if not group:
            return
        jobs = self._job_repo.get_by_group(group_id)
        statuses = [j.status for j in jobs]
        total_frames = group["total_frames"] or 0
        total_rendered = min(
            total_frames,
            sum(j.rendered_frames for j in jobs),
        )
        result = compute_group_status(
            current_group_status=group["status"],
            job_statuses=statuses,
            total_frames=total_frames,
            total_rendered=total_rendered,
        )
        if result.should_persist:
            self._group_repo.update_status(group_id, result.status)
