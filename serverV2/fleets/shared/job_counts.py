"""JobCounts — merges rendered/uploaded counts from DB and Redis.

The DB column ``rendered_frames`` is a worker self-report and can lie;
the ``output_frames`` table is ground truth for completion (PK on
(group_id, filename) — sibling-retry duplicates collapse to one row);
the Redis live counter is the freshest signal of in-flight activity.
This helper centralises how we combine them so Modal and Vast monitors
agree on the semantics.
"""

from __future__ import annotations

from typing import Any

from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.repositories.progress_repository import ProgressRepository


class JobCounts:

    def __init__(
        self,
        progress_repo: ProgressRepository,
        output_frame_repo: OutputFrameRepository,
    ) -> None:
        self._progress = progress_repo
        self._output_frames = output_frame_repo

    def uploaded(self, job: dict[str, Any]) -> int:
        """This worker's verified upload count -- rows in ``output_frames``
        whose ``job_id`` matches.  Used by liveness signals like the
        loading-stall detector's "has this worker uploaded anything yet"
        gate.  Do NOT use for chunk completion -- ``add_many``'s ON
        CONFLICT path hides frames a sibling already uploaded under a
        different job_id.  Use :meth:`is_complete` (chunk-range-based)
        for completion decisions.
        """
        return self._output_frames.count_for_job(job["id"])

    def rendered(self, job_id: str, job: dict[str, Any]) -> int:
        """Best estimate of rendered count for liveness / staleness checks.
        Combines worker-pushed, verified uploads, and Redis progress.  Do
        NOT use for a "done" decision — :meth:`is_complete` is the
        chunk-range-based check that handles sibling retries correctly.
        """
        counts = [
            job.get("rendered_frames") or 0,
            self.uploaded(job),
        ]
        live = self._progress.get(job_id)
        if live is not None:
            counts.append(live.rendered_frames)
        return max(counts)

    def is_complete(self, job: dict[str, Any]) -> bool:
        """Chunk-level completion check: every frame in this job's
        ``[frame_start, frame_end]`` range exists in ``output_frames``
        for the group, regardless of which job_id uploaded each.

        Per-job-id counts (``count_for_job``) silently miss frames a
        sibling retry already uploaded — those rows are deduped under
        the sibling's job_id, so this worker's count never reaches
        ``total_frames`` even though the chunk is actually complete.
        """
        total = job.get("total_frames") or 0
        if total <= 0:
            return False
        group_id = job.get("group_id") or job.get("id") or ""
        if not group_id:
            return False
        frame_start = int(job.get("frame_start") or 0)
        frame_end = int(job.get("frame_end") or 0)
        frame_step = int(job.get("frame_step") or 1)
        covered = self._output_frames.count_in_range(
            group_id, frame_start, frame_end, frame_step,
        )
        return covered >= total
