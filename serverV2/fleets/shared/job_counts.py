"""JobCounts — merges rendered/uploaded counts from DB, Redis, and output_files.

The DB column ``rendered_frames`` is a worker self-report and can lie; the
verified ``output_files`` list is ground truth for completion; the Redis
live counter is the freshest signal of in-flight activity.  This helper
centralises how we combine them so Modal and Vast monitors agree on the
semantics.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.value_objects import parse_output_files
from serverV2.repositories.progress_repository import ProgressRepository


class JobCounts:

    def __init__(self, progress_repo: ProgressRepository) -> None:
        self._progress = progress_repo

    def uploaded(self, job: dict[str, Any]) -> int:
        """Verified upload count — only files registered in ``output_files``.
        Use for completion decisions; worker self-reports don't belong here.
        """
        return len(parse_output_files(job.get("output_files")))

    def rendered(self, job_id: str, job: dict[str, Any]) -> int:
        """Best estimate of rendered count for liveness / staleness checks.
        Combines worker-pushed, verified uploads, and Redis progress.  Do
        NOT use for a "done" decision — only :meth:`uploaded` is trustworthy.
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
        total = job.get("total_frames") or 0
        return total > 0 and self.uploaded(job) >= total
