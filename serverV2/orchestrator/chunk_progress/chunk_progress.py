"""ChunkProgress — value object describing a chunk's frame coverage.

Returned by ``ChunkProgressService.progress_for_chunk``.  Carries the
two fields downstream callers actually need:

* ``is_complete`` — every expected frame has been uploaded by some
  sibling.  Used by the API listing to decide whether to expose a
  Retry button on a chunk.
* ``remaining_range`` — ``(start, end, step)`` covering the earliest
  missing frame through the canonical chunk's end (contiguous shape;
  mid-chunk gaps are absorbed into this range rather than scattered).
  ``None`` when the chunk is complete OR when there are no siblings.
  Used by the retry pipeline to know what to dispatch.

Empty siblings produce ``(is_complete=False, remaining_range=None)`` —
an undefined-state encoding.  Callers that distinguish "empty" from
"complete" must check siblings themselves before calling the service.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkProgress:
    is_complete: bool
    remaining_range: tuple[int, int, int] | None
