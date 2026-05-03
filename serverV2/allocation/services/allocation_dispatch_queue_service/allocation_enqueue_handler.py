"""AllocationEnqueueHandler — write side of the dispatch queue.

One responsibility: pre-generate stable job_ids, gate against the
in-progress dedup ledger, write rows into ``dispatch_queue``, and
return synthesized ``DispatchResult``s the upstream caller can persist
immediately even though dispatch fires asynchronously on the daemon's
next tick.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationDispatchQueueRepository,
)
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_queue_item_codec import (
    AllocationQueueItemCodec,
)
from serverV2.core.models import DispatchContext, DispatchResult, PlannedTask
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository

log = logging.getLogger(__name__)


class AllocationEnqueueHandler:

    def __init__(
        self,
        *,
        queue_repo: AllocationDispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        codec: AllocationQueueItemCodec,
    ) -> None:
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._codec = codec

    def enqueue(
        self,
        group_id: str,
        tasks: list[PlannedTask],
        context: DispatchContext,
    ) -> list[DispatchResult]:
        results: list[DispatchResult] = []
        for t in tasks:
            chunk_index = t.chunk_index or 0
            owner = self._in_progress.current_job_for(group_id, chunk_index)
            if owner is not None:
                log.info(
                    "Group %s chunk %s already in progress (job %s) "
                    "— skipping enqueue",
                    group_id, chunk_index, owner,
                )
                continue
            job_id = str(uuid4())
            self._queue_repo.enqueue(
                group_id, self._codec.encode(t, context, job_id=job_id),
            )
            results.append(
                DispatchResult(
                    job_id=job_id,
                    machine_id=t.machine_id or "",
                    status="queued",
                    provider_job_id=None,
                    error=None,
                )
            )
        return results
