"""ChunkRequest — a pending chunk of work that needs a machine assigned.

Carries everything the allocator needs to pick a machine for one chunk
(used by the retry path and any future single-chunk allocation).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChunkRequest:
    group_id: str
    chunk_index: int
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    attempt: int
    # Extensibility fields for future allocation rules.
    user_id: str | None = None
    excluded_machine_ids: tuple[str, ...] = field(default_factory=tuple)
