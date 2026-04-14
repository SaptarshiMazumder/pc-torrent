"""RenderOrchestrator — Facade.

The single entry point callers use.  Composes FrameAllocator, Dispatcher,
CallbackRouter, and BlendUrlResolver.  No direct DB or HTTP calls.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

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
    ) -> None:
        self._allocator = frame_allocator
        self._dispatcher = dispatcher
        self._callbacks = callback_router
        self._blend_url = blend_url_resolver
        self._job_repo = job_repo

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
    # 2. Execute: dispatch planned tasks to fleet strategies
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

        blend_url = ""
        if tasks:
            blend_url = self._blend_url.resolve(
                tasks[0].machine_type, group_id, input_filename,
            )

        context = DispatchContext(
            group_id=group_id,
            input_filename=input_filename,
            render_overrides_b64=overrides_b64,
            blend_url=blend_url,
            max_retries=scheduling.get("max_retries_per_chunk", 0),
            priority=scheduling.get("priority", 0),
        )

        return self._dispatcher.dispatch_all(tasks, context)

    # ------------------------------------------------------------------
    # 3. Handle failure: retry -> failover -> give up
    # ------------------------------------------------------------------

    def handle_failure(
        self,
        *,
        job: dict[str, Any] | RenderJob,
        error: str,
        group_id: str,
    ) -> str | None:
        raw = job if isinstance(job, dict) else self._job_repo.get_raw_by_id(job.job_id)
        if raw is None:
            return None
        return self._callbacks.handle_failure_from_monitor(raw, error, group_id)

    # ------------------------------------------------------------------
    # 4. Worker callbacks
    # ------------------------------------------------------------------

    def on_job_success(self, job_id: str) -> None:
        self._callbacks.route(job_id=job_id, outcome=CallbackOutcome.SUCCESS)

    def on_job_failure(self, job_id: str, error: str) -> None:
        self._callbacks.route(job_id=job_id, outcome=CallbackOutcome.FAILURE, error=error)

    def on_job_progress(self, job_id: str, rendered_frames: int, total_frames: int) -> None:
        self._callbacks.route(
            job_id=job_id,
            outcome=CallbackOutcome.PROGRESS,
            rendered_frames=rendered_frames,
            total_frames=total_frames,
        )
