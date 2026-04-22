"""RenderOrchestrator — public facade over the orchestration layer.

Routers, fleet monitors, failure/success handlers, and the failover
scanner talk to THIS class.  All methods are one-line delegations to
``RenderLifecycle`` where the actual flow lives.

Keep this file trivial.  If you want to understand what happens during a
render's lifetime, open ``orchestrator/lifecycle.py``.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import DispatchResult, Machine, PlannedTask
from serverV2.orchestrator.lifecycle import RenderLifecycle


class RenderOrchestrator:

    def __init__(self, lifecycle: RenderLifecycle) -> None:
        self._lifecycle = lifecycle

    # ---- planning + execution ----

    def plan(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        machines: list[Machine],
    ) -> list[PlannedTask]:
        return self._lifecycle.plan(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machines=machines,
        )

    def execute(
        self,
        *,
        group_id: str,
        input_filename: str,
        tasks: list[PlannedTask],
        render_overrides_json: str,
    ) -> list[DispatchResult]:
        return self._lifecycle.start_render(
            group_id=group_id,
            input_filename=input_filename,
            tasks=tasks,
            render_overrides_json=render_overrides_json,
        )

    # ---- chunk-level callbacks ----

    def on_job_failed(self, job_id: str, error: str) -> bool:
        return self._lifecycle.handle_chunk_failure(job_id, error)

    def on_job_success(self, job_id: str) -> None:
        self._lifecycle.handle_chunk_success(job_id)

    def on_job_progress(
        self, job_id: str, rendered_frames: int, total_frames: int,
    ) -> None:
        self._lifecycle.record_chunk_progress(job_id, rendered_frames, total_frames)

    # ---- cancellation ----

    def cancel_group(self, group_id: str) -> dict[str, Any]:
        return self._lifecycle.cancel_render(group_id)
