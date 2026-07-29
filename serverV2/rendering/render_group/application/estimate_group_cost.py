"""Retrieval: frozen-at-planning cost summary for a group.

Sums the per-chunk estimates the allocation planner stamped on each chunk
at planning time -- static for the group's lifetime.  Reads chunk data
through the chunk repository; the summation itself is a domain calculation.
Returns all-zero when the group has no chunks yet (still parked on the
pending queue), which the caller reads as "Queued".
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.chunk_repository import IChunkRepository


class EstimateGroupCost:
    def __init__(self, *, chunk_repo: IChunkRepository) -> None:
        self._chunk_repo = chunk_repo

    def execute(self, group_id: str) -> dict:
        raise NotImplementedError
