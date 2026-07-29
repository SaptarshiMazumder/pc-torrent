"""Callback: react to a chunk reporting failure.

Ask allocation whether the chunk can be re-planned (a retry re-enters the
pending queue), then funnel into ``ReconcileRenderGroupStatus``.  The group
must NOT flip to ``failed`` while a retry is still in flight -- that guard
lives in the rollup rule via ``has_work_in_flight``, which is exactly why the
reconcile step re-reads allocation state.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.reconcile_render_group_status import (
    ReconcileRenderGroupStatus,
)
from serverV2.rendering.render_group.application.ports.allocation_service import IAllocationService
from serverV2.rendering.render_group.application.ports.chunk_repository import IChunkRepository


class FailureHandler:
    def __init__(
        self,
        *,
        chunk_repo: IChunkRepository,
        allocation: IAllocationService,
        reconcile: ReconcileRenderGroupStatus,
    ) -> None:
        self._chunk_repo = chunk_repo
        self._allocation = allocation
        self._reconcile = reconcile

    def execute(self, job_id: str, *, error: str) -> None:
        raise NotImplementedError
