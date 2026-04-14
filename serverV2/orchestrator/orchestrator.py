"""RenderOrchestrator — Facade.

The single entry point callers use.  Composes FrameAllocator, Dispatcher,
CallbackRouter, BlendUrlResolver, and DispatchQueueManager.

All dispatch logic (initial + retry) lives here.  The queue is a dumb
container; the orchestrator decides when and where to dispatch.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Any, Callable

from serverV2.allocation.power_scorer import compute_power_score
from serverV2.callbacks.router import CallbackRouter
from serverV2.core.enums import CallbackOutcome
from serverV2.core.models import (
    DispatchContext,
    DispatchResult,
    Machine,
    PlannedTask,
    RenderJob,
)
from serverV2.orchestrator.blend_url_resolver import BlendUrlResolver
from serverV2.orchestrator.dispatch_queue import (
    DispatchQueueManager,
    GroupDispatchQueue,
    QueueItem,
)
from serverV2.orchestrator.dispatcher import Dispatcher
from serverV2.orchestrator.frame_allocator import FrameAllocator
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)


class RenderOrchestrator:

    def __init__(
        self,
        frame_allocator: FrameAllocator,
        dispatcher: Dispatcher,
        callback_router: CallbackRouter,
        blend_url_resolver: BlendUrlResolver,
        job_repo: JobRepository,
        queue_manager: DispatchQueueManager,
        machine_picker: Callable[[], list[Machine]],
    ) -> None:
        self._allocator = frame_allocator
        self._dispatcher = dispatcher
        self._callbacks = callback_router
        self._blend_url = blend_url_resolver
        self._job_repo = job_repo
        self._queue_mgr = queue_manager
        self._machine_picker = machine_picker
        self._requeued_jobs: set[str] = set()
        self._requeue_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 1. Plan: split frames across machines
    # ------------------------------------------------------------------

    def plan(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        machines: list[Machine],
    ) -> list[PlannedTask]:
        return self._allocator.allocate(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machines=machines,
        )

    # ------------------------------------------------------------------
    # 2. Execute: enqueue planned tasks and flush the queue
    # ------------------------------------------------------------------

    def execute(
        self,
        *,
        group_id: str,
        input_filename: str,
        tasks: list[PlannedTask],
        render_overrides_json: str,
        scheduling: dict[str, Any],
    ) -> list[DispatchResult]:
        overrides_b64 = base64.b64encode(render_overrides_json.encode()).decode()
        max_retries = scheduling.get("max_retries_per_chunk", 0)

        queue = self._queue_mgr.create(group_id, max_retries)

        items = [
            QueueItem(
                frame_start=t.frame_start,
                frame_end=t.frame_end,
                frame_step=t.frame_step,
                total_frames=t.total_frames,
                chunk_index=t.chunk_index,
            )
            for t in tasks
        ]
        queue.enqueue_all(items)

        context = DispatchContext(
            group_id=group_id,
            input_filename=input_filename,
            render_overrides_b64=overrides_b64,
            blend_url="",
            max_retries=max_retries,
            priority=scheduling.get("priority", 0),
        )

        return self._flush_queue(queue, context, initial_tasks=tasks)

    # ------------------------------------------------------------------
    # 3. Failure handling: requeue remaining frames
    # ------------------------------------------------------------------

    def on_job_failed(self, job_id: str, error: str) -> None:
        """Called after a job is marked failed. Decides whether to requeue."""
        with self._requeue_lock:
            if job_id in self._requeued_jobs:
                log.info("Job %s already requeued — ignoring duplicate failure", job_id)
                return
            self._requeued_jobs.add(job_id)

        raw = self._job_repo.get_raw_by_id(job_id)
        if not raw:
            return

        rj = RenderJob.from_row(raw)
        group_id = rj.group_id

        if rj.status not in ("failed",):
            return

        from serverV2.infrastructure.db import query_one
        grp = query_one("SELECT status FROM render_groups WHERE id = %s", (group_id,))
        if grp and grp.get("status") in ("cancelled", "done"):
            log.info("Group %s is %s — not requeuing job %s", group_id, grp["status"], job_id)
            return

        remaining = rj.remaining_frames()
        if remaining is None:
            return

        queue = self._queue_mgr.get(group_id)
        if queue is None:
            queue = self._queue_mgr.create(group_id, rj.max_retries)

        next_attempt = (rj.attempt or 0) + 1
        if next_attempt > queue.max_retries:
            log.warning("Job %s: max retries (%d) exhausted for frames %d-%d",
                        job_id, queue.max_retries, remaining[0], remaining[1])
            return

        frame_start, frame_end = remaining
        item = QueueItem(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=rj.frame_step,
            total_frames=((frame_end - frame_start) // rj.frame_step) + 1,
            attempt=next_attempt,
            chunk_index=rj.chunk_index,
        )
        queue.enqueue(item)
        log.info("Job %s: requeued frames %d-%d (attempt %d/%d)",
                 job_id, frame_start, frame_end, next_attempt, queue.max_retries)

        overrides_b64 = base64.b64encode(
            (rj.render_overrides_json or "{}").encode()
        ).decode()
        context = DispatchContext(
            group_id=group_id,
            input_filename=rj.input_filename,
            render_overrides_b64=overrides_b64,
            blend_url="",
            max_retries=rj.max_retries,
            priority=rj.priority,
        )

        self._flush_queue(queue, context)

    # ------------------------------------------------------------------
    # 4. Worker callbacks (routed through CallbackRouter)
    # ------------------------------------------------------------------

    def on_job_success(self, job_id: str) -> None:
        self._callbacks.route(job_id=job_id, outcome=CallbackOutcome.SUCCESS)

    def on_job_progress(self, job_id: str, rendered_frames: int, total_frames: int) -> None:
        self._callbacks.route(
            job_id=job_id,
            outcome=CallbackOutcome.PROGRESS,
            rendered_frames=rendered_frames,
            total_frames=total_frames,
        )

    # ------------------------------------------------------------------
    # 5. Cancellation
    # ------------------------------------------------------------------

    def cancel_group(self, group_id: str) -> None:
        """Drain the dispatch queue so no more retries happen."""
        queue = self._queue_mgr.get(group_id)
        if queue:
            drained = queue.drain()
            if drained:
                log.info("Group %s: drained %d items from dispatch queue", group_id, len(drained))
            self._queue_mgr.remove(group_id)

    # ------------------------------------------------------------------
    # Internal: flush queue and pick machines
    # ------------------------------------------------------------------

    def _flush_queue(
        self,
        queue: GroupDispatchQueue,
        context: DispatchContext,
        initial_tasks: list[PlannedTask] | None = None,
    ) -> list[DispatchResult]:
        """Dequeue all items, assign machines, dispatch."""
        results: list[DispatchResult] = []
        idx = 0

        while True:
            item = queue.dequeue()
            if item is None:
                break

            if initial_tasks and idx < len(initial_tasks):
                task = initial_tasks[idx]
            else:
                machine = self._pick_machine()
                if machine is None:
                    log.error("No available machine for frames %d-%d (group %s)",
                              item.frame_start, item.frame_end, queue.group_id)
                    continue
                task = PlannedTask(
                    machine_id=machine.id,
                    machine_type=machine.machine_type,
                    gpu_model=machine.gpu_model,
                    gpu_vram_gb=machine.gpu_vram_gb,
                    frame_start=item.frame_start,
                    frame_end=item.frame_end,
                    frame_step=item.frame_step,
                    total_frames=item.total_frames,
                    power_score=compute_power_score(machine),
                    chunk_index=item.chunk_index,
                )

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

            try:
                result = self._dispatcher.dispatch_one(task, dispatch_ctx)
                results.append(result)
            except Exception as exc:
                log.error("Dispatch failed for frames %d-%d (group %s): %s",
                          item.frame_start, item.frame_end, queue.group_id, exc)

            idx += 1

        return results

    def _pick_machine(self) -> Machine | None:
        machines = self._machine_picker()
        if not machines:
            return None
        return max(machines, key=compute_power_score)
