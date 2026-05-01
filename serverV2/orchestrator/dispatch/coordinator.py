"""DispatchCoordinator — the mechanical plumbing behind a dispatch.

After Phase 2 of the allocator redesign, the coordinator is also the
gatekeeper for fleet-cap enforcement: tasks that don't fit within the
target fleet's headroom sit in the DB-backed ``dispatch_queue`` and are
drained later by event-driven calls to :meth:`drain_for_fleet` from the
success/failure handlers.

Makes no allocation decisions and no retry decisions.  Pure plumbing
plus capacity gating.
"""

from __future__ import annotations

import json
import logging
from typing import Callable
from uuid import uuid4

from serverV2.core.models import DispatchContext, DispatchResult, PlannedTask
from serverV2.orchestrator.blend_url_resolver import BlendUrlResolver
from serverV2.orchestrator.dispatch.dispatcher import Dispatcher
from serverV2.repositories.dispatch_queue_repository import (
    DispatchQueueRepository,
    QueueItem,
)
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)


class DispatchCoordinator:

    def __init__(
        self,
        *,
        queue_repo: DispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        dispatcher: Dispatcher,
        blend_url_resolver: BlendUrlResolver,
        job_repo: JobRepository,
        fleet_cap_lookup: Callable[[str], int],
    ) -> None:
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._dispatcher = dispatcher
        self._blend_url = blend_url_resolver
        self._job_repo = job_repo
        self._fleet_cap_lookup = fleet_cap_lookup

    # ------------------------------------------------------------------
    # initial dispatch (called by lifecycle.start_render and retry path)
    # ------------------------------------------------------------------

    def enqueue_and_flush(
        self,
        group_id: str,
        tasks: list[PlannedTask],
        context: DispatchContext,
    ) -> list[DispatchResult]:
        """Enqueue tasks, then dispatch as many as the per-fleet cap allows.

        Tasks that don't fit stay in the queue tagged by ``fleet`` and are
        drained later by :meth:`drain_for_fleet` when a job ends.

        **Pre-enqueue ledger check**: skip any task whose chunk slot is
        already owned by an active job (in_progress_chunks has a row for
        ``(group_id, chunk_index)``).  Combined with the DB-level UNIQUE
        constraint on dispatch_queue, this is the dedup gate that
        prevents duplicate dispatches for the same chunk.
        """
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
            self._queue_repo.enqueue(group_id, _queue_item_for(t, context))

        in_flight = dict(self._job_repo.count_active_by_fleet())
        results: list[DispatchResult] = []
        for t in tasks:
            cap = self._fleet_cap_lookup(t.fleet)
            current = in_flight.get(t.fleet, 0)
            if current >= cap:
                log.info(
                    "Group %s: %s at cap (%d/%d) — chunk %s queued",
                    group_id, t.fleet, current, cap, t.chunk_index,
                )
                continue
            popped = self._queue_repo.dequeue_for_fleet(t.fleet)
            if popped is None:
                continue
            in_flight[t.fleet] = current + 1
            result = self._dispatch_queue_item(popped, group_id_hint=group_id)
            if result is not None:
                results.append(result)
        return results

    # ------------------------------------------------------------------
    # event-driven drain (called by SuccessHandler / FailureHandler)
    # ------------------------------------------------------------------

    def drain_for_fleet(self, fleet: str) -> int:
        """Pull queued items targeting ``fleet`` and dispatch as many as
        the current cap allows.  Returns the number dispatched.
        """
        cap = self._fleet_cap_lookup(fleet)
        dispatched = 0
        while True:
            current = self._job_repo.count_active_by_fleet().get(fleet, 0)
            if current >= cap:
                break
            item = self._queue_repo.dequeue_for_fleet(fleet)
            if item is None:
                break
            result = self._dispatch_queue_item(item)
            if result is not None:
                dispatched += 1
        if dispatched:
            log.info("drain_for_fleet(%s): dispatched %d queued chunk(s)", fleet, dispatched)
        return dispatched

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _dispatch_queue_item(
        self, item: QueueItem, *, group_id_hint: str | None = None,
    ) -> DispatchResult | None:
        group_id = group_id_hint or item.group_id
        if not group_id:
            log.warning(
                "Queue item missing group_id (chunk %s) — dropping",
                item.chunk_index,
            )
            return None

        task = _task_from_queue_item(item)
        blend_url = self._blend_url.resolve(task.fleet, group_id, item.input_filename)
        dispatch_ctx = DispatchContext(
            group_id=group_id,
            input_filename=item.input_filename,
            render_overrides_json=item.render_overrides_json,
            blend_url=blend_url,
            max_retries=item.max_retries,
            priority=item.priority,
            engine=_engine_from_json(item.render_overrides_json),
        )

        # Pre-generate job_id + claim the in-progress ledger BEFORE dispatch
        # so the retry chain's stale-signal guard can deduplicate correctly
        # if dispatch fails synchronously.
        job_id = str(uuid4())
        self._in_progress.claim_or_replace(
            group_id=group_id,
            chunk_index=task.chunk_index or 0,
            job_id=job_id,
            attempt=task.attempt,
        )

        try:
            return self._dispatcher.dispatch_one(task, dispatch_ctx, job_id=job_id)
        except Exception as exc:
            log.error(
                "Dispatch failed for frames %d-%d (group %s): %s",
                task.frame_start, task.frame_end, group_id, exc,
            )
            return None


# ----------------------------------------------------------------------
# helpers (module-level so they don't pollute the class)
# ----------------------------------------------------------------------

def _queue_item_for(task: PlannedTask, context: DispatchContext) -> QueueItem:
    return QueueItem(
        frame_start=task.frame_start,
        frame_end=task.frame_end,
        frame_step=task.frame_step,
        total_frames=task.total_frames,
        fleet=task.fleet,
        label=task.label,
        vram_gb=task.vram_gb,
        render_speed=task.render_speed,
        gpu_type=task.gpu_type,
        machine_id=task.machine_id,
        input_filename=context.input_filename,
        render_overrides_json=context.render_overrides_json,
        max_retries=context.max_retries,
        priority=context.priority,
        attempt=task.attempt,
        chunk_index=task.chunk_index,
    )


def _task_from_queue_item(item: QueueItem) -> PlannedTask:
    return PlannedTask(
        fleet=item.fleet,
        machine_id=item.machine_id,
        gpu_type=item.gpu_type,
        label=item.label,
        vram_gb=item.vram_gb,
        render_speed=item.render_speed,
        frame_start=item.frame_start,
        frame_end=item.frame_end,
        frame_step=item.frame_step,
        total_frames=item.total_frames,
        chunk_index=item.chunk_index,
        attempt=item.attempt,
    )


def _engine_from_json(render_overrides_json: str | None) -> str | None:
    """Return ``render.engine`` from a JSON-encoded overrides payload, or
    None if the payload is empty.  Raises on malformed JSON — at this
    point in the pipeline the payload was produced by trusted upstream
    code, so a parse failure is a real bug and should surface."""
    if not render_overrides_json:
        return None
    parsed = json.loads(render_overrides_json)
    if not isinstance(parsed, dict):
        return None
    render_section = parsed.get("render")
    if not isinstance(render_section, dict):
        return None
    engine = render_section.get("engine")
    return engine if isinstance(engine, str) and engine else None
