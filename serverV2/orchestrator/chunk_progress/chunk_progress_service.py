"""ChunkProgressService — single source of truth for "is this chunk
done?" / "what frames are still missing?"

Composes one DB read (``OutputFrameRepository.frame_numbers_for_chunk``)
with a pure in-process computation.  The chunk-level frame-number query
lives on ``OutputFrameRepository``; the service just calls it.

Algorithm (verbatim transplant of the legacy
``ComputeRemainingFramesStep._compute``):

1. Empty siblings → ``(is_complete=False, remaining_range=None)``.
2. Pick the canonical sibling = ``min(siblings, key=attempt or 0)``.
3. Read the chunk's expected ``(frame_start, frame_end, frame_step)``
   from the canonical row (the original chunk's range, before any
   sub-range retries narrowed it).
4. Read the rendered frame numbers straight from the generated
   ``frame_number`` column (parsed from each filename's ``frame####``
   token at write time).  Rows without a frame token are NULL and never
   reach this set.
5. Set difference: ``expected_frames - rendered_frames``.
6. Empty difference → ``(is_complete=True, remaining_range=None)``.
7. Non-empty → ``(is_complete=False,
   remaining_range=(missing[0], chunk_end, step))``.  Mid-chunk gaps
   are absorbed into this contiguous range rather than scattered into
   N parallel single-frame retries.

Callers supply ``siblings`` because they already have the list in
scope — forcing the service to re-query would just add a redundant
``get_raw_by_group`` round-trip on every call.
"""

from __future__ import annotations

from typing import Any

from serverV2.orchestrator.chunk_progress.chunk_progress import ChunkProgress
from serverV2.repositories.output_frame_repository import OutputFrameRepository


class ChunkProgressService:

    def __init__(self, *, output_frame_repo: OutputFrameRepository) -> None:
        self._output_frames = output_frame_repo

    def progress_for_chunk(
        self,
        group_id: str,
        chunk_index: int,
        siblings: list[dict[str, Any]],
    ) -> ChunkProgress:
        rendered_frames = self._output_frames.frame_numbers_for_chunk(
            group_id, chunk_index,
        )
        return self._compute(siblings, rendered_frames)

    def progress_for_group(
        self,
        group_id: str,
        jobs: list[dict[str, Any]],
    ) -> dict[int, ChunkProgress]:
        """Bulk equivalent of ``{ci: progress_for_chunk(...)}`` for every
        chunk_index in the group.  One DB query (``unique_filenames_per_
        chunk_for_group``) instead of N -- used by the render-groups
        detail page so the per-chunk computation collapses to a single
        round-trip.

        Returns a dict mapping each chunk_index seen in ``jobs`` to its
        ``ChunkProgress``.  Chunks with no siblings (shouldn't happen in
        practice -- caller passes the group's full job list) are absent
        from the result.
        """
        siblings_by_chunk: dict[int, list[dict[str, Any]]] = {}
        for j in jobs:
            ci = j.get("chunk_index") or 0
            siblings_by_chunk.setdefault(ci, []).append(j)

        frames_by_chunk = self._output_frames.frame_numbers_per_chunk_for_group(
            group_id,
        )
        return {
            ci: self._compute(siblings, frames_by_chunk.get(ci, set()))
            for ci, siblings in siblings_by_chunk.items()
        }

    @staticmethod
    def _compute(
        siblings: list[dict[str, Any]],
        rendered_frames: set[int],
    ) -> ChunkProgress:
        """Pure in-process computation shared by ``progress_for_chunk``
        (singular, used by the retry pipeline) and ``progress_for_group``
        (bulk, used by the detail page).  No I/O.

        ``rendered_frames`` is the set of frame indices already in
        ``output_frames`` for this chunk, read from the generated
        ``frame_number`` column upstream — no filename parsing here.
        """
        if not siblings:
            return ChunkProgress(is_complete=False, remaining_range=None)

        original = min(siblings, key=lambda j: j.get("attempt") or 0)
        chunk_start = int(original.get("frame_start") or 0)
        chunk_end = int(original.get("frame_end") or 0)
        step = int(original.get("frame_step") or 1)

        all_chunk_frames = set(range(chunk_start, chunk_end + 1, step))
        missing = sorted(all_chunk_frames - rendered_frames)
        if not missing:
            return ChunkProgress(is_complete=True, remaining_range=None)
        return ChunkProgress(
            is_complete=False,
            remaining_range=(missing[0], chunk_end, step),
        )
