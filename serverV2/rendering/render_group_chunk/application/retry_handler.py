"""Callback: manually retry a single chunk (user-triggered).

Distinct from the automatic retry inside ``FailureHandler``: this is a user
command against one chunk.  It re-submits the chunk to allocation and
announces the change so the live UI updates immediately, then leaves the
eventual group rollup to the reconcile chokepoint.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.allocation_service import IAllocationService
from serverV2.rendering.render_group.application.ports.group_status_gateway import IGroupStatusGateway
from serverV2.rendering.render_group.application.ports.chunk_repository import IChunkRepository


class RetryHandler:
    def __init__(
        self,
        *,
        chunk_repo: IChunkRepository,
        allocation: IAllocationService,
        notifier: IGroupStatusGateway,
    ) -> None:
        self._chunk_repo = chunk_repo
        self._allocation = allocation
        self._notifier = notifier

    def execute(self, job_id: str) -> None:
        raise NotImplementedError
