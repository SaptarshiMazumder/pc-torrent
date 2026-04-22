"""RenderLifecycle — the readable narrative of a render's lifetime.

Every method on this class tells one top-to-bottom story:

    start_render           — user submitted a render
    handle_chunk_failure   — a worker's chunk failed
    handle_chunk_success   — a worker's chunk finished
    record_chunk_progress  — a worker reported progress
    cancel_render          — user cancelled the render

All decisions (stale-signal guard, retry-attempt limit, cancellation
teardown order) live here.  Grunt work is delegated:

    FrameAllocator       — picks machines for chunks (initial + retry)
    DispatchCoordinator  — drains the queue, claims the ledger, dispatches
    GroupStatusAggregator (pure function) — computes group-level status

Open THIS file to understand what happens during a render.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Callable

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
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest
from serverV2.orchestrator.allocation.frame_allocator import FrameAllocator
from serverV2.orchestrator.config import MAX_RETRIES
from serverV2.orchestrator.dispatch.coordinator import DispatchCoordinator
from serverV2.repositories.dispatch_queue_repository import DispatchQueueRepository
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


class RenderLifecycle:

    def __init__(
        self,
        *,
        allocator: FrameAllocator,
        coordinator: DispatchCoordinator,
        callback_router: CallbackRouter,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        machine_repo: MachineRepository,
        queue_repo: DispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        fleet_registry: FleetRegistry,
        machine_picker: Callable[[], list[Machine]],
    ) -> None:
        self._allocator = allocator
        self._coordinator = coordinator
        self._callbacks = callback_router
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._machine_repo = machine_repo
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._fleet = fleet_registry
        self._machine_picker = machine_picker

    # ------------------------------------------------------------------
    # Planning — split frames across machines (no dispatch)
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
        return self._allocator.allocate_initial(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machines=machines,
        )

    # ------------------------------------------------------------------
    # Story 1: user submitted a render
    # ------------------------------------------------------------------

    def start_render(
        self,
        *,
        group_id: str,
        input_filename: str,
        tasks: list[PlannedTask],
        render_overrides_json: str,
    ) -> list[DispatchResult]:
        # Safety net: don't dispatch into a group that went terminal between
        # submission and execution (e.g. user cancelled immediately).
        grp = self._group_repo.get_by_id(group_id)
        if grp and grp.get("status") in ("cancelled", "done"):
            log.info("Group %s is %s — refusing to dispatch", group_id, grp["status"])
            return []

        overrides_b64 = base64.b64encode(render_overrides_json.encode()).decode()
        context = DispatchContext(
            group_id=group_id,
            input_filename=input_filename,
            render_overrides_b64=overrides_b64,
            blend_url="",
            max_retries=MAX_RETRIES,
            priority=0,
        )
        return self._coordinator.enqueue_and_flush(group_id, tasks, context)

    # ------------------------------------------------------------------
    # Story 2: a chunk failed
    # ------------------------------------------------------------------

    def handle_chunk_failure(self, job_id: str, error: str) -> bool:
        """Called BEFORE a job is marked failed.  Returns True if a retry
        was dispatched, False if the chunk is giving up."""
        raw = self._job_repo.get_raw_by_id(job_id)
        if not raw:
            return False

        rj = RenderJob.from_row(raw)
        group_id = rj.group_id
        chunk_index = rj.chunk_index or 0

        # Stale-signal guard: if the failing job is no longer the active
        # attempt for its chunk, a retry has already been dispatched and
        # we must not spawn another one.
        current_job_for_chunk = self._in_progress.current_job_for(group_id, chunk_index)
        if current_job_for_chunk is not None and current_job_for_chunk != job_id:
            log.info(
                "Stale failure signal for job %s (chunk %s now owned by %s)",
                job_id, chunk_index, current_job_for_chunk,
            )
            return False

        grp = self._group_repo.get_by_id(group_id)
        if grp and grp.get("status") in ("cancelled", "done"):
            log.info("Group %s is %s — not requeuing job %s", group_id, grp["status"], job_id)
            return False

        remaining = rj.remaining_frames()
        if remaining is None:
            self._in_progress.release(group_id, chunk_index)
            return False

        next_attempt = (rj.attempt or 0) + 1
        if next_attempt > MAX_RETRIES:
            log.warning(
                "Job %s: max retries (%d) exhausted for frames %d-%d",
                job_id, MAX_RETRIES, remaining[0], remaining[1],
            )
            self._in_progress.release(group_id, chunk_index)
            return False

        frame_start, frame_end = remaining
        total_frames = ((frame_end - frame_start) // rj.frame_step) + 1

        chunk_request = ChunkRequest(
            group_id=group_id,
            chunk_index=chunk_index,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=rj.frame_step,
            total_frames=total_frames,
            attempt=next_attempt,
        )
        retry_task = self._allocator.allocate_retry(chunk_request, self._machine_picker())
        if retry_task is None:
            log.error(
                "Job %s: no eligible machine for retry of frames %d-%d (group %s)",
                job_id, frame_start, frame_end, group_id,
            )
            return False

        log.info(
            "Job %s: requeued frames %d-%d (attempt %d/%d) on machine %s",
            job_id, frame_start, frame_end, next_attempt, MAX_RETRIES,
            retry_task.machine_id,
        )

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
        self._coordinator.enqueue_and_flush(group_id, [retry_task], context)
        return True

    # ------------------------------------------------------------------
    # Story 3: a chunk finished
    # ------------------------------------------------------------------

    def handle_chunk_success(self, job_id: str) -> None:
        self._callbacks.route(job_id=job_id, outcome=CallbackOutcome.SUCCESS)

    # ------------------------------------------------------------------
    # Story 4: a worker reported progress
    # ------------------------------------------------------------------

    def record_chunk_progress(
        self, job_id: str, rendered_frames: int, total_frames: int,
    ) -> None:
        self._callbacks.route(
            job_id=job_id,
            outcome=CallbackOutcome.PROGRESS,
            rendered_frames=rendered_frames,
            total_frames=total_frames,
        )

    # ------------------------------------------------------------------
    # Story 5: user cancelled the render
    # ------------------------------------------------------------------

    def cancel_render(self, group_id: str) -> dict[str, Any]:
        """Mark cancelled → stop monitors → drain queue + ledger → cancel
        provider-side jobs.  Order matters: marking the group cancelled
        first blocks any in-flight requeues via the DB guard."""
        # 1. Mark group + active jobs cancelled in the DB
        self._group_repo.update_status(group_id, "cancelled")
        jobs = self._job_repo.get_active_by_group(group_id)
        for job in jobs:
            self._job_repo.update_status(job["id"], "cancelled", error="Cancelled by user")

        # 2. Stop monitor threads so they stop acting on this group
        for job in jobs:
            mt = self._machine_repo.get_type(job["machine_id"])
            strategy = self._fleet.get(mt)
            if strategy:
                strategy.stop_monitoring(job["id"])

        # 3. Drain the dispatch queue + in-progress ledger for the group
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
