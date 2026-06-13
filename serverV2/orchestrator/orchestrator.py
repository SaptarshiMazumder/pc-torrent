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

from typing import Any, Callable

from serverV2.allocation.services.allocation_planning_service import (
    GroupCostEstimate,
)
from serverV2.core.value_objects import RENDER_PRIORITY_DEFAULT
from serverV2.orchestrator.lifecycle import RenderLifecycle
from serverV2.orchestrator.task_actual_cost import TaskActualCost
from serverV2.orchestrator.users_client import UsersClient


class RenderOrchestrator:

    def __init__(
        self,
        lifecycle: RenderLifecycle,
        *,
        users_client: UsersClient,
        task_actual_cost: TaskActualCost,
        get_priority_multiplier: Callable[[int], float],
    ) -> None:
        self._lifecycle = lifecycle
        self._users = users_client
        self._cost = task_actual_cost
        # Fresh-config lookup so an admin tuning multipliers in
        # ConfigurationPage takes effect immediately on the next
        # estimate / display / billing read.  Defaults to NORMAL when
        # the priority is out of range.
        self._get_priority_multiplier = get_priority_multiplier

    # ---- per-row actual cost (UI display path) ----

    def actual_cost_for_row(
        self,
        *,
        started_at: Any,
        completed_at: Any,
        price_per_hour: Any,
        status: str,
        priority: int = RENDER_PRIORITY_DEFAULT,
    ) -> tuple[float | None, float | None]:
        """Single source of truth for ``(actual_seconds, actual_cost_usd)``.
        The serializer goes through here so the UI value matches the
        number ``UsersClient`` debits when the chunk lands terminal.

        The priority cost multiplier is applied here so every consumer
        of the actual cost (UI display, billing) sees the priced-as-
        billed number.  Seconds are NOT multiplied -- the chunk takes
        the same wall time regardless of priority.
        """
        seconds, base_cost = self._cost.compute(
            started_at=started_at,
            completed_at=completed_at,
            price_per_hour=price_per_hour,
            status=status,
        )
        if base_cost is None or base_cost <= 0:
            return seconds, base_cost
        multiplier = self._get_priority_multiplier(priority)
        return seconds, base_cost * multiplier

    # ---- cost intelligence ----

    def cost_estimate_for_group(self, group_id: str) -> GroupCostEstimate:
        """Post-submit live group: SUM of per-chunk estimates the planner
        stamped on every jobs row.  Static for the lifetime of the group."""
        return self._lifecycle.cost_estimate_for_group(group_id)

    def cost_estimate_for_dry_run(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        engine: str | None = None,
        heaviness: dict | None = None,
        priority: int = RENDER_PRIORITY_DEFAULT,
    ) -> GroupCostEstimate:
        """Pre-submit cost preview for a hypothetical group.  ``priority``
        flows into the cost aggregator's multiplier so the preview matches
        what the user will actually be billed.
        """
        return self._lifecycle.cost_estimate_for_dry_run(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            engine=engine,
            heaviness=heaviness,
            priority=priority,
        )

    def get_queue_depth(self) -> dict:
        """Per-fleet, per-priority counts of items waiting in the
        pending + dispatch queues.  Used by the Create Render page to
        show the user what's ahead at each priority level.
        """
        return self._lifecycle.get_queue_depth()

    def submit_initial(
        self,
        *,
        group_id: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        machine_ids: list[str] | None,
        heaviness: dict | None,
        engine: str | None,
        tier: str | None,
        input_filename: str,
        render_overrides_json: str,
        priority: int = RENDER_PRIORITY_DEFAULT,
    ) -> None:
        """Park a whole render group on ``pending_allocation_queue``.
        Returns nothing -- the daemon plans + dispatches asynchronously
        on its next tick.  See ``Lifecycle.submit_initial`` and
        ``AllocationFacade.submit_initial`` for details.

        ``priority`` is the user-selected queue ordering key (LOW / NORMAL
        / HIGH; see ``core.value_objects``).  Defaults to NORMAL so
        callers that don't surface it to the user keep today's behaviour.
        """
        self._lifecycle.submit_initial(
            group_id=group_id,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machine_ids=machine_ids,
            heaviness=heaviness,
            engine=engine,
            tier=tier,
            input_filename=input_filename,
            render_overrides_json=render_overrides_json,
            priority=priority,
        )

    # ---- chunk-level callbacks (full flow — adapters call these) ----

    def on_job_failed(self, job_id: str, error: str) -> None:
        """A job reported failure (worker self-report or monitor detection).
        Lifecycle decides retry vs terminal, marks the job failed, drains
        the failed fleet's queue, and rolls the change up to the group.

        After lifecycle returns, the user's spend is brought up to date
        for this chunk.  When lifecycle went terminal, this seals the
        final number; when lifecycle retried instead, this catches any
        spend accrued since the last tick.
        """
        self._lifecycle.handle_chunk_failed(job_id, error)
        self._users.update_spend_for_chunk_by_id(job_id)

    def on_job_succeeded(self, job_id: str) -> None:
        """A job reported success.  Lifecycle marks it done, releases the
        in-progress ledger, writes telemetry, drains the fleet's queue,
        and rolls the change up to the group.

        After lifecycle returns, the user's spend is brought up to
        the final number for this chunk.
        """
        self._lifecycle.handle_chunk_succeeded(job_id)
        self._users.update_spend_for_chunk_by_id(job_id)

    def update_spend_for_chunk(self, row: dict[str, Any]) -> None:
        """Per-monitor-tick hook: recompute live cost for a still-running
        chunk and bring the user's recorded spend up to that number.

        The monitor already fetched ``row`` (with ``owner_uid`` joined
        from ``render_groups``) for its own decision logic; we re-use
        it so the hot tick path does no extra repository work.
        Idempotent and converging: each call charges only the delta
        since last call thanks to the high-water-mark marker doc.
        """
        self._users.update_spend_for_chunk(row)

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

    # ---- community machine reports idle (post-register / post-job) ----

    def handle_community_machine_idle(self, machine_id: str) -> None:
        """Community PC just signalled it's idle and ready for work.  Any
        running jobs still assigned to it must be from a prior session
        the agent lost (sidecar rebuild, crash, etc.) — fail them so
        retries fire."""
        self._lifecycle.handle_community_machine_idle(machine_id)

    # ---- user-triggered retry of a stuck chunk ----

    def retry_chunk_manually(self, job_id: str) -> dict[str, Any]:
        """User pressed "Retry" on a stuck chunk.  Validates the chunk is
        actually stuck (failed + auto-retries exhausted + nothing active)
        and dispatches a fresh attempt for the un-uploaded frames only.
        Raises ``ManualRetryError`` on refusal."""
        return self._lifecycle.retry_chunk_manually(job_id)

    # ---- cancellation ----

    def cancel_group(self, group_id: str) -> dict[str, Any]:
        result = self._lifecycle.cancel_render(group_id)
        # Mid-render chunks are now ``cancelled`` with completed_at set.
        # Seal each chunk's spend at its final number; pending chunks
        # produce zero-cost no-ops; already-terminal chunks whose
        # marker is at the final number produce zero-delta no-ops.
        self._users.update_spend_for_group_jobs(group_id)
        return result

    def cancel_one_job(self, job_id: str) -> dict[str, Any]:
        """User pressed Cancel on a single in-flight chunk (B2).
        Idempotent; no-op on unknown / already-terminal jobs."""
        result = self._lifecycle.cancel_one_job(job_id)
        self._users.update_spend_for_chunk_by_id(job_id)
        return result
