"""RenderOrchestrator — Facade.

The single entry point callers use.  Composes FrameAllocator, Dispatcher,
CallbackRouter, BlendUrlResolver, and DispatchQueueRepository.

All dispatch logic (initial + retry) lives here.  The queue is a DB table;
the orchestrator decides when and where to dispatch.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Callable
from uuid import uuid4

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
from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.blend_url_resolver import BlendUrlResolver
from serverV2.orchestrator.config import MAX_RETRIES
from serverV2.orchestrator.dispatcher import Dispatcher
from serverV2.orchestrator.frame_allocator import FrameAllocator
from serverV2.repositories.dispatch_queue_repository import (
    DispatchQueueRepository,
    QueueItem,
)
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


class RenderOrchestrator:

    def __init__(
        self,
        frame_allocator: FrameAllocator,
        dispatcher: Dispatcher,
        callback_router: CallbackRouter,
        blend_url_resolver: BlendUrlResolver,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        machine_repo: MachineRepository,
        fleet_registry: FleetRegistry,
        queue_repo: DispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        machine_picker: Callable[[], list[Machine]],
    ) -> None:
        self._allocator = frame_allocator
        self._dispatcher = dispatcher
        self._callbacks = callback_router
        self._blend_url = blend_url_resolver
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._machine_repo = machine_repo
        self._fleet = fleet_registry
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._machine_picker = machine_picker

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
    ) -> list[DispatchResult]:
        overrides_b64 = base64.b64encode(render_overrides_json.encode()).decode()

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
        self._queue_repo.enqueue_all(group_id, items)

        context = DispatchContext(
            group_id=group_id,
            input_filename=input_filename,
            render_overrides_b64=overrides_b64,
            blend_url="",
            max_retries=MAX_RETRIES,
            priority=0,
        )

        return self._flush_queue(group_id, context, initial_tasks=tasks)

    # ------------------------------------------------------------------
    # 3. Failure handling: requeue remaining frames
    # ------------------------------------------------------------------

    def on_job_failed(self, job_id: str, error: str) -> bool:
        """Called BEFORE a job is marked failed.  Decides whether to requeue.

        Returns True if a retry was dispatched, False if retries are exhausted
        OR the signal is stale (the chunk has already moved on to a newer job).
        """
        raw = self._job_repo.get_raw_by_id(job_id)
        if not raw:
            return False

        rj = RenderJob.from_row(raw)
        group_id = rj.group_id

        # Stale-signal guard: if this failing job is no longer the active
        # attempt for its chunk, a retry has already been dispatched and we
        # must not spawn another one.
        current_job_for_chunk = self._in_progress.current_job_for(group_id, rj.chunk_index or 0)
        if current_job_for_chunk is not None and current_job_for_chunk != job_id:
            log.info(
                "Stale failure signal for job %s (chunk %s now owned by %s)",
                job_id, rj.chunk_index, current_job_for_chunk,
            )
            return False

        grp = self._group_repo.get_by_id(group_id)
        if grp and grp.get("status") in ("cancelled", "done"):
            log.info("Group %s is %s — not requeuing job %s", group_id, grp["status"], job_id)
            return False

        remaining = rj.remaining_frames()
        if remaining is None:
            self._in_progress.release(group_id, rj.chunk_index or 0)
            return False

        next_attempt = (rj.attempt or 0) + 1
        if next_attempt > MAX_RETRIES:
            log.warning("Job %s: max retries (%d) exhausted for frames %d-%d",
                        job_id, MAX_RETRIES, remaining[0], remaining[1])
            self._in_progress.release(group_id, rj.chunk_index or 0)
            return False

        frame_start, frame_end = remaining
        item = QueueItem(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=rj.frame_step,
            total_frames=((frame_end - frame_start) // rj.frame_step) + 1,
            attempt=next_attempt,
            chunk_index=rj.chunk_index,
        )
        self._queue_repo.enqueue(group_id, item)
        log.info("Job %s: requeued frames %d-%d (attempt %d/%d)",
                 job_id, frame_start, frame_end, next_attempt, MAX_RETRIES)

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

        self._flush_queue(group_id, context)
        return True

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

    def cancel_group(self, group_id: str) -> dict[str, Any]:
        """Full cancel: stop monitors, mark DB, drain queue, kill providers."""
        # 1. Mark group + jobs cancelled in DB (blocks requeues via DB guard)
        self._group_repo.update_status(group_id, "cancelled")
        jobs = self._job_repo.get_active_by_group(group_id)
        for job in jobs:
            self._job_repo.update_status(job["id"], "cancelled", error="Cancelled by user")

        # 2. Stop all monitor threads for these jobs — kills the polling loops
        for job in jobs:
            mt = self._machine_repo.get_type(job["machine_id"])
            strategy = self._fleet.get(mt)
            if strategy:
                strategy.stop_monitoring(job["id"])

        # 3. Drain dispatch queue + in-progress ledger
        drained = self._queue_repo.drain(group_id)
        if drained:
            log.info("Group %s: drained %d items from dispatch queue", group_id, drained)
        self._in_progress.release_all(group_id)

        # 4. Cancel provider-side jobs (Modal function calls, Vast instances)
        for job in jobs:
            mt = self._machine_repo.get_type(job["machine_id"])
            strategy = self._fleet.get(mt)
            if not strategy or not strategy.is_enabled():
                continue
            pid = strategy.provider_job_id_from_job(job)
            if not pid:
                continue
            try:
                strategy.cancel(pid, job["machine_id"])
            except Exception as exc:
                log.warning("Failed to cancel provider job %s: %s", pid, exc)

        log.info("Group %s: cancelled %d jobs", group_id, len(jobs))
        return {"cancelled_jobs": len(jobs)}

    # ------------------------------------------------------------------
    # Internal: flush queue and pick machines
    # ------------------------------------------------------------------

    def _flush_queue(
        self,
        group_id: str,
        context: DispatchContext,
        initial_tasks: list[PlannedTask] | None = None,
    ) -> list[DispatchResult]:
        """Dequeue all items from DB, assign machines, dispatch."""
        grp = self._group_repo.get_by_id(group_id)
        if grp and grp.get("status") in ("cancelled", "done"):
            log.info("Group %s is %s — refusing to dispatch", group_id, grp["status"])
            return []

        results: list[DispatchResult] = []
        idx = 0

        while True:
            item = self._queue_repo.dequeue(group_id)
            if item is None:
                break

            if initial_tasks and idx < len(initial_tasks):
                task = initial_tasks[idx]
            else:
                machine = self._pick_machine()
                if machine is None:
                    log.error("No available machine for frames %d-%d (group %s)",
                              item.frame_start, item.frame_end, group_id)
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
                    attempt=item.attempt,
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

            # Pre-generate job_id and claim the chunk in the in-progress ledger
            # BEFORE dispatching.  If the strategy's synchronous failure path
            # fires during dispatch, the retry chain's stale-signal guard can
            # see this claim and deduplicate correctly.
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
                log.error("Dispatch failed for frames %d-%d (group %s): %s",
                          item.frame_start, item.frame_end, group_id, exc)

            idx += 1

        return results

    def _pick_machine(self) -> Machine | None:
        machines = self._machine_picker()
        if not machines:
            return None
        return max(machines, key=compute_power_score)
