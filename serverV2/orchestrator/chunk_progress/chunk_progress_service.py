"""ChunkProgressService — single source of truth for "is this chunk
done?" / "what frames are still missing?"

Composes one DB read (``OutputFrameRepository.unique_filenames_for_chunk``)
with a pure in-process computation.  No new SQL — the chunk-level
filenames query already lives on ``OutputFrameRepository`` and stays
there; the service just calls it.

Algorithm (verbatim transplant of the legacy
``ComputeRemainingFramesStep._compute``):

1. Empty siblings → ``(is_complete=False, remaining_range=None)``.
2. Pick the canonical sibling = ``min(siblings, key=attempt or 0)``.
3. Read the chunk's expected ``(frame_start, frame_end, frame_step)``
   from the canonical row (the original chunk's range, before any
   sub-range retries narrowed it).
4. Parse frame numbers from the rendered filenames using the
   ``frame####.<ext>`` filename convention.  Filenames that don't
   match are silently dropped (no production renderer produces those;
   defensive against accidental uploads).
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

import re
from typing import Any

from serverV2.orchestrator.chunk_progress.chunk_progress import ChunkProgress
from serverV2.repositories.output_frame_repository import OutputFrameRepository


_FRAME_FILENAME_RE = re.compile(r"frame(\d+)\.")


class ChunkProgressService:

    def __init__(self, *, output_frame_repo: OutputFrameRepository) -> None:
        self._output_frames = output_frame_repo

    def progress_for_chunk(
        self,
        group_id: str,
        chunk_index: int,
        siblings: list[dict[str, Any]],
    ) -> ChunkProgress:
        if not siblings:
            return ChunkProgress(is_complete=False, remaining_range=None)

        original = min(siblings, key=lambda j: j.get("attempt") or 0)
        chunk_start = int(original.get("frame_start") or 0)
        chunk_end = int(original.get("frame_end") or 0)
        step = int(original.get("frame_step") or 1)

        rendered_filenames = self._output_frames.unique_filenames_for_chunk(
            group_id, chunk_index,
        )
        rendered: set[int] = set()
        for fname in rendered_filenames:
            match = _FRAME_FILENAME_RE.match(fname)
            if match:
                rendered.add(int(match.group(1)))

        all_chunk_frames = set(range(chunk_start, chunk_end + 1, step))
        missing = sorted(all_chunk_frames - rendered)
        if not missing:
            return ChunkProgress(is_complete=True, remaining_range=None)
        return ChunkProgress(
            is_complete=False,
            remaining_range=(missing[0], chunk_end, step),
        )
