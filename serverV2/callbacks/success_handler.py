"""SuccessHandler — marks a job done and reconciles group status."""

from __future__ import annotations

import logging

from serverV2.callbacks.group_status_aggregator import compute_group_status
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


class SuccessHandler:

    def __init__(
        self,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        in_progress_repo: InProgressChunkRepository,
    ) -> None:
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._in_progress = in_progress_repo

    def handle(self, job_id: str, group_id: str) -> None:
        # Release the chunk from the in-progress ledger FIRST so any late
        # failure signal for this job is recognized as stale and ignored.
        raw = self._job_repo.get_raw_by_id(job_id)
        if raw is not None:
            chunk_index = raw.get("chunk_index") or 0
            self._in_progress.release(group_id, chunk_index)

        self._job_repo.mark_done(job_id)
        log.info("Job %s marked done", job_id)
        self._reconcile_group(group_id)

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
