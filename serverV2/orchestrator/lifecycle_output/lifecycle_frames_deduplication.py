"""LifecycleFramesDeduplication — authoritative rendered-frame accounting.

Multiple workers can render the same frame within one chunk's lifetime
(race-induced double-dispatch, manual retry firing while auto-retry is
in flight, monitor-restart edge cases, etc.).  They all upload to the
same R2 key (last-write-wins; only one file actually exists), but
each job's ``output_files`` lists the filename separately.  Summing
``rendered_frames`` across siblings double-counts.

This class treats the data as the source of truth: a frame is rendered
iff its filename appears in some sibling's ``output_files``.  Set-union
collapses duplicates; the resulting count is honest regardless of how
many workers happened to render each frame.

Two consumers in RenderLifecycle:

* Retry paths (``_try_dispatch_retry`` and ``retry_chunk_manually``)
  ask for the actually-missing frame range so the new attempt only
  re-renders frames not already covered.
* Group rollup paths (``reconcile_group_status`` and the cancel-render
  snapshot) ask for the total unique frame count so the group's
  progress reflects real coverage instead of inflated dispatch counts.
"""

from __future__ import annotations

import re

from serverV2.core.models import RenderJob
from serverV2.core.value_objects import parse_output_files
from serverV2.repositories.job_repository import JobRepository

_FRAME_FILENAME_RE = re.compile(r"frame(\d+)\.")


class LifecycleFramesDeduplication:

    def __init__(self, *, job_repo: JobRepository) -> None:
        self._job_repo = job_repo

    def compute_remaining_for_chunk(
        self, group_id: str, chunk_index: int,
    ) -> tuple[int, int, int] | None:
        """Returns ``(frame_start, frame_end, frame_step)`` of the actually-
        missing frame range for this chunk, computed from the union of
        ``output_files`` across ALL sibling attempts.  Returns None if
        the chunk has no missing frames (every frame in the original
        range has been rendered by some attempt).

        The returned range is ``(first_missing_frame, chunk_end, step)``:
        the next retry covers from the earliest gap to the end of the
        chunk.  Mid-chunk gaps (rare in practice -- workers render
        sequentially) get included via this contiguous shape rather than
        scattered into N parallel single-frame retries.
        """
        raw_jobs = [
            j for j in self._job_repo.get_raw_by_group(group_id)
            if (j.get("chunk_index") or 0) == chunk_index
        ]
        if not raw_jobs:
            return None

        # Use the original (lowest attempt) sibling's frame range as the
        # canonical chunk range.  Later attempts may have been dispatched
        # with a subsetted range from a partial-progress retry; the
        # original spans the whole chunk.
        original = min(raw_jobs, key=lambda j: j.get("attempt") or 0)
        chunk_start = int(original.get("frame_start") or 0)
        chunk_end = int(original.get("frame_end") or 0)
        step = int(original.get("frame_step") or 1)

        rendered: set[int] = set()
        for j in raw_jobs:
            for fname in parse_output_files(j.get("output_files")):
                n = self._frame_number_from_filename(fname)
                if n is not None:
                    rendered.add(n)

        all_chunk_frames = set(range(chunk_start, chunk_end + 1, step))
        missing = sorted(all_chunk_frames - rendered)
        if not missing:
            return None

        return (missing[0], chunk_end, step)

    @staticmethod
    def compute_total_rendered(jobs: list[RenderJob]) -> int:
        """Count UNIQUE rendered frame filenames across every job in the
        group.  Two jobs both rendering ``frame0016.png`` count as 1.
        Used for group-level total_rendered so progress reflects actual
        output coverage, not how many workers happened to render each
        frame."""
        seen: set[str] = set()
        for j in jobs:
            seen.update(parse_output_files(j.output_files))
        return len(seen)

    @staticmethod
    def _frame_number_from_filename(fname: str) -> int | None:
        """Extract the integer frame number from ``frame####.png``.
        Returns None for filenames that don't match the convention --
        we just skip those rather than fail the whole computation."""
        match = _FRAME_FILENAME_RE.match(fname)
        return int(match.group(1)) if match else None
