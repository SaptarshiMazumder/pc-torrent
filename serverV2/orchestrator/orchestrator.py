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
        heaviness: dict | None = None,
        engine: str | None = None,
        tier: str | None = None,
    ) -> list[PlannedTask]:
        """Plan an initial allocation.

        ``machine_ids``: when set, restrict allocation to those community
        machines (serverless fleets are excluded — the user picked specific
        boxes).  When None, the full pool of community machines + enabled
        serverless capabilities is considered.

        ``heaviness``: parsed analysis_snapshot["heaviness"] dict with
        ``file_size_bytes`` injected (see ``parse_analysis_heaviness``).
        Used by cost-aware strategies (FastRender, Economy) for both
        target selection and the soft budget cap.  ``None`` is fine —
        treated as a defaulted dict with file_size=0.

        ``engine``: render engine string ("BLENDER_EEVEE", "CYCLES", ...).
        Forwarded to the strategy's TargetValidators for fleet-compatibility
        filtering.

        ``tier``: user-selected allocation tier ("economy" | "standard" |
        "premium").  Routes to the matching allocator and (for Standard)
        derives a soft budget cap.  ``None`` defaults to "standard".
        """
        return self._lifecycle.plan(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machine_ids=machine_ids,
            heaviness=heaviness,
            engine=engine,
            tier=tier,
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

    # ---- chunk-level callbacks (full flow — adapters call these) ----

    def on_job_failed(self, job_id: str, error: str) -> None:
        """A job reported failure (worker self-report or monitor detection).
        Lifecycle decides retry vs terminal, marks the job failed, drains
        the failed fleet's queue, and rolls the change up to the group.
        """
        self._lifecycle.handle_chunk_failed(job_id, error)

    def on_job_succeeded(self, job_id: str) -> None:
        """A job reported success.  Lifecycle marks it done, releases the
        in-progress ledger, writes telemetry, drains the fleet's queue,
        and rolls the change up to the group.
        """
        self._lifecycle.handle_chunk_succeeded(job_id)

    def on_job_progress(
        self, job_id: str, rendered_frames: int, total_frames: int,
    ) -> None:
        """Worker pushed a PROGRESS event.  On the first one, transition
        pending → running and roll up to the group."""
        self._lifecycle.handle_chunk_progress(job_id, rendered_frames, total_frames)

    def on_job_running(self, job_id: str) -> None:
        """Fleet monitor saw the container reach 'running' state before
        the worker had a chance to send PROGRESS — transition the job
        from pending to running and roll up to the group."""
        self._lifecycle.handle_chunk_running(job_id)

    # ---- read facade (used by monitors instead of direct repo imports) ----

    def is_job_terminal(self, job_id: str) -> bool:
        return self._lifecycle.is_job_terminal(job_id)

    def get_job_status(self, job_id: str) -> str | None:
        return self._lifecycle.get_job_status(job_id)

    def get_group_status(self, group_id: str) -> str | None:
        return self._lifecycle.get_group_status(group_id)

    def get_job_raw(self, job_id: str) -> dict[str, Any] | None:
        """Read-only job row for monitors that need fields like
        ``rendered_frames``, ``machine_type``, ``error``."""
        return self._lifecycle.get_job_raw(job_id)

    # ---- cancellation ----

    def cancel_group(self, group_id: str) -> dict[str, Any]:
        return self._lifecycle.cancel_render(group_id)
