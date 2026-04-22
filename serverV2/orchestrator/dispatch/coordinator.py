"""DispatchCoordinator — the mechanical plumbing behind a dispatch.

Given a list of fully-allocated ``PlannedTask`` (machine already chosen by
the allocator), this drains the dispatch queue, pre-generates job ids,
claims the in-progress ledger, and routes each task through the
``Dispatcher``.

Makes no allocation decisions and no retry decisions.  Pure plumbing.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from serverV2.core.models import DispatchContext, DispatchResult, PlannedTask
from serverV2.orchestrator.blend_url_resolver import BlendUrlResolver
from serverV2.orchestrator.dispatch.dispatcher import Dispatcher
from serverV2.repositories.dispatch_queue_repository import (
    DispatchQueueRepository,
    QueueItem,
)
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository

log = logging.getLogger(__name__)


class DispatchCoordinator:

    def __init__(
        self,
        *,
        queue_repo: DispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        dispatcher: Dispatcher,
        blend_url_resolver: BlendUrlResolver,
    ) -> None:
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._dispatcher = dispatcher
        self._blend_url = blend_url_resolver

    def enqueue_and_flush(
        self,
        group_id: str,
        tasks: list[PlannedTask],
        context: DispatchContext,
    ) -> list[DispatchResult]:
        """Enqueue queue items for the given tasks, then dispatch all of them.

        Each ``PlannedTask`` arrives with its machine already chosen by the
        allocator; this method does not select machines.
        """
        items = [
            QueueItem(
                frame_start=t.frame_start,
                frame_end=t.frame_end,
                frame_step=t.frame_step,
                total_frames=t.total_frames,
                chunk_index=t.chunk_index,
                attempt=t.attempt,
            )
            for t in tasks
        ]
        self._queue_repo.enqueue_all(group_id, items)
        return self._flush(group_id, context, tasks)

    def _flush(
        self,
        group_id: str,
        context: DispatchContext,
        tasks: list[PlannedTask],
    ) -> list[DispatchResult]:
        results: list[DispatchResult] = []
        idx = 0

        while True:
            item = self._queue_repo.dequeue(group_id)
            if item is None:
                break

            if idx >= len(tasks):
                log.warning(
                    "Group %s: dequeued an item with no matching task (idx=%d). "
                    "Dropping it — this should not happen.", group_id, idx,
                )
                break
            task = tasks[idx]
            idx += 1

            blend_url = self._blend_url.resolve(
                task.machine_type, context.group_id, context.input_filename,
            )
            dispatch_ctx = DispatchContext(
                group_id=context.group_id,
                input_filename=context.input_filename,
                render_overrides_b64=context.render_overrides_b64,
                blend_url=blend_url,
                max_retries=context.max_retries,
                priority=context.priority,
            )

            # Pre-generate job_id and claim the chunk in the in-progress
            # ledger BEFORE dispatch.  If the strategy's synchronous failure
            # path fires during dispatch, the retry chain's stale-signal
            # guard can see this claim and deduplicate correctly.
            job_id = str(uuid4())
            self._in_progress.claim_or_replace(
                group_id=context.group_id,
                chunk_index=task.chunk_index or 0,
                job_id=job_id,
                attempt=task.attempt,
            )

            try:
                result = self._dispatcher.dispatch_one(task, dispatch_ctx, job_id=job_id)
                results.append(result)
            except Exception as exc:
                log.error(
                    "Dispatch failed for frames %d-%d (group %s): %s",
                    task.frame_start, task.frame_end, group_id, exc,
                )

        return results
