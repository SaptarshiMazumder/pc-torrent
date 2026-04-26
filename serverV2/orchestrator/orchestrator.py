"""RenderOrchestrator — public facade over the orchestration layer.

Owns every state transition that touches a ``render_group``: planning,
initial execution, failure (retry coordination), cancellation, and the
group-status rollup that follows any job state change.  Callback handlers
notify the orchestrator via ``on_job_succeeded`` / ``on_job_started`` /
``on_job_failed_terminal`` — they never touch ``render_groups`` directly.

Keep this file trivial.  If you want to understand what happens during a
render's lifetime, open ``orchestrator/lifecycle.py``.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import DispatchResult, PlannedTask
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
        machine_ids: list[str] | None = None,
        file_size_bytes: int | None = None,
        engine: str | None = None,
    ) -> list[PlannedTask]:
        """Plan an initial allocation.

        ``machine_ids``: when set, restrict allocation to those community
        machines (serverless fleets are excluded — the user picked specific
        boxes).  When None, the full pool of community machines + enabled
        serverless capabilities is considered.

        ``file_size_bytes``: blend file size, used as the heaviness signal
        by FastRenderAllocationStrategy.  ``None`` falls back to Default.

        ``engine``: render engine string ("BLENDER_EEVEE", "CYCLES", ...).
        Forwarded to the strategy's TargetValidators for fleet-compatibility
        filtering.
        """
        return self._lifecycle.plan(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machine_ids=machine_ids,
            file_size_bytes=file_size_bytes,
            engine=engine,
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
        """Try to retry the failed chunk; return True if retry dispatched."""
        return self._lifecycle.handle_chunk_failure(job_id, error)

    def on_job_started(self, group_id: str) -> None:
        """A job transitioned pending → running; roll up to the group."""
        self._lifecycle.reconcile_group_status(group_id)

    def on_job_succeeded(self, group_id: str) -> None:
        """A job was marked done; roll up to the group (e.g. last child → done)."""
        self._lifecycle.reconcile_group_status(group_id)

    def on_job_failed_terminal(self, group_id: str) -> None:
        """A job was marked failed and retries are exhausted; roll up to
        the group (e.g. last child → failed)."""
        self._lifecycle.reconcile_group_status(group_id)

    # ---- cancellation ----

    def cancel_group(self, group_id: str) -> dict[str, Any]:
        return self._lifecycle.cancel_render(group_id)
