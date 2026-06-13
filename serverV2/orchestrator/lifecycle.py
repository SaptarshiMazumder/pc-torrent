"""RenderLifecycle — the readable narrative of a render's lifetime.

Every method on this class tells one top-to-bottom story:

    start_render             — user submitted a render
    handle_chunk_failure     — a worker's chunk failed (decides retry)
    cancel_render            — user cancelled the render
    reconcile_group_status   — a job state changed; roll up to the group

Group-level state is owned by this layer.  Callback handlers update only
the job row, then notify the orchestrator (success / failure-after-retry /
first progress) so this lifecycle can update the parent group.

All decisions (stale-signal guard, retry-attempt limit, cancellation
teardown order, group rollup) live here.  Grunt work is delegated:

    AllocationClient     — gateway to the allocation module (plan + enqueue)
    GroupStatusAggregator (pure function) — computes group-level status

Open THIS file to understand what happens during a render.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from serverV2.allocation.allocation_strategies.allocation_helpers import allocation_tiers as tiers
from serverV2.allocation.services.allocation_planning_service import (
    GroupCostEstimate,
)
from serverV2.callbacks.group_status_aggregator import compute_group_status
from serverV2.core.models import (
    DispatchContext,
    DispatchResult,
    RenderJob,
)
from serverV2.core.value_objects import (
    RENDER_PRIORITY_DEFAULT,
    clamp_render_priority,
)
from serverV2.fleets.modal.modal_active_jobs_hooks import ModalActiveJobsHooks
from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.allocation_client import AllocationClient
from serverV2.orchestrator.anti_affinity import AntiAffinityFacade
from serverV2.orchestrator.repositories import (
    DispatchAllocationRepository,
    PendingAllocationRepository,
)
from serverV2.orchestrator.lifecycle_job_retry import (
    MANUAL_RETRY_PIPELINE,
    RetryExecutor,
)
from serverV2.orchestrator.lifecycle_job_termination import (
    CANCEL_PIPELINE,
    FAILURE_PIPELINE,
    JobTerminator,
    RenderCanceler,
)
from serverV2.orchestrator.lifecycle_job_termination.execution.terminal_group_resource_releaser import (
    TerminalGroupResourceReleaser,
)
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.services.machines.machine_repository import MachineRepository
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository
from serverV2.repositories.telemetry_repository import TelemetryRepository

log = logging.getLogger(__name__)

_TERMINAL_GROUP_STATUSES = frozenset({"done", "failed", "cancelled"})


# ``ManualRetryError`` and ``RETRY_REASON_HTTP_STATUS`` live in the
# lifecycle_job_retry package alongside the manual retry pipeline.
# Re-exported here so existing imports (api/routers/jobs.py) keep working;
# new callers should import directly from lifecycle_job_retry.
from serverV2.orchestrator.lifecycle_job_retry import (  # noqa: E402  (re-export)
    RETRY_REASON_HTTP_STATUS,
    ManualRetryError,
)


class RenderLifecycle:

    def __init__(
        self,
        *,
        allocation_client: AllocationClient,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        machine_repo: MachineRepository,
        in_progress_repo: InProgressChunkRepository,
        telemetry_repo: TelemetryRepository,
        output_frame_repo: OutputFrameRepository,
        pending_allocation_repo: PendingAllocationRepository,
        dispatch_allocation_repo: DispatchAllocationRepository,
        fleet_registry: FleetRegistry,
        retry_executor: RetryExecutor,
        anti_affinity: AntiAffinityFacade,
        modal_active_jobs_hooks: ModalActiveJobsHooks,
        job_terminator: JobTerminator,
        render_canceler: RenderCanceler,
        terminal_group_resource_releaser: TerminalGroupResourceReleaser,
        get_max_retries: Callable[[], int],
    ) -> None:
        self._allocation_client = allocation_client
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._machine_repo = machine_repo
        self._in_progress = in_progress_repo
        self._telemetry = telemetry_repo
        self._output_frames = output_frame_repo
        self._pending_allocation_repo = pending_allocation_repo
        self._dispatch_allocation_repo = dispatch_allocation_repo
        self._fleet = fleet_registry
        self._retry_executor = retry_executor
        self._anti_affinity = anti_affinity
        self._modal_active_jobs_hooks = modal_active_jobs_hooks
        self._terminator = job_terminator
        self._render_canceler = render_canceler
        self._terminal_group_resource_releaser = terminal_group_resource_releaser
        # Reads orchestrator.max_retries fresh from Firestore each call
        # so a desktop ConfigurationPage edit takes effect on the next
        # submission instead of waiting for a server restart.
        self._get_max_retries = get_max_retries

    # ------------------------------------------------------------------
    # Cost intelligence — pass-through to the allocation module.
    # The pre-render preview and the live-group cost endpoint both flow
    # through here so monitors / routers / services never import the
    # allocation client directly.
    # ------------------------------------------------------------------

    def cost_estimate_for_group(self, group_id: str) -> GroupCostEstimate:
        return self._allocation_client.cost_estimate_for_group(group_id)

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
        return self._allocation_client.cost_estimate_for_dry_run(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            engine=engine,
            heaviness=heaviness,
            priority=priority,
        )

    def get_queue_depth(self) -> dict:
        return self._allocation_client.get_queue_depth()


    def _fire_modal_terminal_hook_if_modal(
        self, raw: dict[str, Any], job_id: str,
    ) -> None:
        """Fire the Modal active-jobs ``on_terminal`` hook iff this row
        is a Modal job.  Called from every terminal-transition entry
        point on this class (success / failure / per-job cancel).
        SREM-backed and idempotent at the tracker layer, so duplicate
        fires from race conditions are harmless."""
        if (raw.get("machine_type") or "") != "modal_serverless":
            return
        gpu_type = (raw.get("gpu_type") or "").strip()
        if not gpu_type:
            return
        self._modal_active_jobs_hooks.on_terminal(
            job_id=job_id, gpu_type=gpu_type,
        )

    # ------------------------------------------------------------------
    # Story 1: user submitted a render
    # ------------------------------------------------------------------

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
        """Submit a whole render group: park to ``pending_allocation_queue``.

        The daemon plans + enqueues against its tick-local mutable
        snapshot on its next tick.  Returns nothing -- the caller's
        confirm-upload response just acks the submission, then the UI
        polls ``GET /render-groups/{id}`` for live chunk state as the
        daemon dispatches.

        ``priority`` is the user-selected queue ordering key.  The clamp
        here is the belt-and-braces fallback for callers that bypass
        the API's Pydantic validator (tests, programmatic submits).
        """
        resolved_tier = tiers.normalize(tier)
        resolved_priority = clamp_render_priority(priority)
        dispatch_context = DispatchContext(
            group_id=group_id,
            input_filename=input_filename,
            render_overrides_json=render_overrides_json,
            blend_url="",
            max_retries=self._get_max_retries(),
            priority=resolved_priority,
            engine=engine,
        )
        self._allocation_client.submit_initial(
            group_id=group_id,
            tier=resolved_tier,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            engine=engine,
            heaviness=heaviness,
            machine_ids=machine_ids,
            dispatch_context=dispatch_context,
        )

    # ------------------------------------------------------------------
    # Story 2: a chunk failed (full flow — retry decision + DB mutations)
    # ------------------------------------------------------------------

    def handle_chunk_failed(self, job_id: str, error: str) -> None:
        """Single entry point for chunk-failure routing.  Thin shim over
        ``JobTerminator.execute(FAILURE_PIPELINE, ...)``.  The full
        narrative (atomic CAS dedup, retry attempt, mark failed,
        release machine, log outcome, reconcile, drain) is encoded as
        the FAILURE_PIPELINE constant in
        ``lifecycle_job_termination/pipelines.py``.

        Adapters (callbacks, monitors) call
        ``RenderOrchestrator.on_job_failed`` which delegates here.
        """
        log.info("[RETRY_DEBUG] handle_chunk_failed(%s) entered", job_id)
        raw = self._job_repo.get_raw_by_id(job_id)
        if raw is None:
            log.info("[RETRY_DEBUG] handle_chunk_failed: job %s NOT FOUND in DB — skipping pipeline", job_id)
            log.info("handle_chunk_failed: job %s not found, skipping", job_id)
            return
        log.info(
            "[RETRY_DEBUG] handle_chunk_failed(%s): raw loaded status=%s attempt=%s chunk_index=%s",
            job_id, raw.get("status"), raw.get("attempt"), raw.get("chunk_index"),
        )
        self._fire_modal_terminal_hook_if_modal(raw, job_id)
        exclusions = self._anti_affinity.exclusions_for_chunk(
            raw.get("group_id") or "",
            raw.get("chunk_index") or 0,
            including_row=raw,
        )
        log.info(
            "[RETRY_DEBUG] handle_chunk_failed(%s): exclusions resolved (machine_ids=%d, capabilities=%d) — invoking FAILURE_PIPELINE",
            job_id,
            len(exclusions.excluded_machine_ids),
            len(exclusions.excluded_serverless_capabilities),
        )
        try:
            self._terminator.execute(
                pipeline=FAILURE_PIPELINE,
                job_id=job_id,
                raw=raw,
                error=error,
                exclusions=exclusions,
            )
            log.info("[RETRY_DEBUG] handle_chunk_failed(%s): FAILURE_PIPELINE completed cleanly", job_id)
        except Exception as exc:
            log.error("[RETRY_DEBUG] handle_chunk_failed(%s): FAILURE_PIPELINE RAISED: %r", job_id, exc)
            raise

    # ------------------------------------------------------------------
    # Story 2c1: community machine reports idle (post-register / post-job)
    # ------------------------------------------------------------------

    def handle_community_machine_idle(self, machine_id: str) -> None:
        """A community machine just signalled "I'm idle, ready for work"
        (via ``PUT /machines/{id}/available``).  Going-idle implies the
        agent has nothing in flight, so any ``status='running'`` jobs
        still tied to this machine fall into one of two cases:

          1. **Chunk actually finished** — agent uploaded all frames,
             then signalled idle, but the CommunityMonitor's next tick
             hasn't fired ``is_complete`` yet (~10s latency).  Route
             this through the success path so the chunk is marked
             ``done`` rather than spuriously failed.
          2. **Chunk genuinely abandoned** — agent restarted, OOM'd,
             or otherwise lost local state mid-render.  Route through
             the failure path so retry kicks in.

        Per-chunk decision is made by ``JobCounts.is_complete``, which
        is sibling-aware (chunk-frame-range coverage rather than
        per-job_id count).
        """
        if not machine_id:
            return
        abandoned = self._job_repo.get_running_for_machine(machine_id)
        for row in abandoned:
            job_id = row.get("id")
            if not job_id:
                continue
            total = int(row.get("total_frames") or 0)
            if total > 0:
                covered = self._output_frames.count_in_range(
                    row.get("group_id") or "",
                    int(row.get("frame_start") or 0),
                    int(row.get("frame_end") or 0),
                    int(row.get("frame_step") or 1),
                )
            else:
                covered = 0
            if total > 0 and covered >= total:
                log.info(
                    "Community job %s on idle machine %s has all frames "
                    "uploaded -- routing through on_success",
                    job_id, machine_id,
                )
                self.handle_chunk_succeeded(job_id)
            else:
                log.info(
                    "Reclaiming abandoned community job %s on machine %s",
                    job_id, machine_id,
                )
                self.handle_chunk_failed(
                    job_id,
                    "Agent went idle while job was running — previous session lost",
                )

    # ------------------------------------------------------------------
    # Story 2d: user-triggered retry of a stuck chunk
    # ------------------------------------------------------------------

    def retry_chunk_manually(self, job_id: str) -> dict[str, Any]:
        """User pressed "Retry" on a stuck chunk in the UI.  Thin shim
        over ``RetryExecutor.execute(MANUAL_RETRY_PIPELINE, ...)``.

        The two ``not_found`` checks below happen pre-pipeline because
        the anti-affinity resolver needs a valid ``group_id`` to query;
        every other validation (group_cancelled, active_sibling_exists,
        not_retryable, no_remaining_frames) is enforced by the pipeline
        as it runs.  ``no_eligible_target`` is no longer a synchronous
        refusal -- the manual-retry pipeline parks the request on
        ``pending_allocation_queue`` and the daemon plans a target on
        its next tick (or leaves it parked if no target is available).

        Anti-affinity is the union of every prior failed/cancelled
        attempt of this chunk — resolved here before the pipeline,
        threaded in via ``RetryContext.exclusions``.

        Raises ``ManualRetryError`` with a stable reason code on refusal.
        """
        raw = self._job_repo.get_raw_by_id(job_id)
        if not raw:
            raise ManualRetryError("not_found")
        group_id = raw.get("group_id") or ""
        if not group_id:
            raise ManualRetryError("not_found")
        chunk_index = raw.get("chunk_index") or 0

        exclusions = self._anti_affinity.exclusions_for_chunk(group_id, chunk_index)
        retry_ctx = self._retry_executor.execute(
            pipeline=MANUAL_RETRY_PIPELINE,
            raw=raw,
            exclusions=exclusions,
        )
        return retry_ctx.result

    # ------------------------------------------------------------------
    # Story 2b: a chunk succeeded (full flow — DB writes + telemetry + drain + rollup)
    # ------------------------------------------------------------------

    def handle_chunk_succeeded(self, job_id: str) -> None:
        """Single entry point for chunk-success routing.  Releases the
        in-progress ledger first (so any late failure signal for this job
        is recognized as stale), marks the job done, writes a telemetry
        row, drains the fleet's queue, and rolls the change up to the group.

        Adapters call ``RenderOrchestrator.on_job_succeeded`` which delegates
        here — they never touch repositories themselves.
        """
        raw = self._job_repo.get_raw_by_id(job_id)
        if raw is None:
            log.warning("on_job_succeeded for unknown job %s", job_id)
            return

        self._fire_modal_terminal_hook_if_modal(raw, job_id)

        fleet = (raw.get("machine_type") or "")
        group_id = (raw.get("group_id") or "")
        chunk_index = raw.get("chunk_index") or 0

        if group_id:
            self._in_progress.release(group_id, chunk_index)

        self._job_repo.mark_done(job_id)
        log.info("Job %s marked done", job_id)

        # Eager provider-side cleanup.  The fleet monitor's next tick would
        # eventually call this, but Vast's runtype=args auto-restarts the
        # container in the meantime (re-downloading the blend, billing for
        # a duplicate render until the monitor catches up) and Modal keeps
        # billing the FunctionCall until cancelled.  Doing it here closes
        # the window from up-to-30s to milliseconds.
        # Best-effort — try/except + the singleton fleet monitor stays as
        # the safety net.  Community has no provider call (provider_job_id
        # is empty), so the short-circuit no-ops; the agent's cancel-status
        # poll handles the community equivalent.
        # No stop_monitoring call: with singleton fleet monitors there are
        # no per-job threads to stop -- the next sweep tick observes the
        # terminal status and drops in-memory state.
        try:
            provider_job_id = (
                raw.get("vast_job_id")
                or raw.get("modal_function_call_id")
                or ""
            )
            if fleet and provider_job_id:
                strategy = self._fleet.get(fleet)
                if strategy is not None:
                    strategy.cancel(provider_job_id)
        except Exception as exc:
            log.warning(
                "Eager provider cleanup for job %s failed (monitor will "
                "catch up): %s", job_id, exc,
            )

        # Release the community machine lock so the allocator can return
        # this PC for the next dispatch.  Vast/Modal jobs have no
        # machines row, so this is a community-only side effect.
        machine_id = raw.get("machine_id")
        if fleet == "windows" and machine_id:
            self._machine_repo.update_status(machine_id, "available")

        try:
            self._record_telemetry(job_id, group_id, raw)
        except Exception as exc:
            # Telemetry must never break the success path.
            log.warning("Telemetry write failed for job %s: %s", job_id, exc)

        if group_id:
            self.reconcile_group_status(group_id)
            # Drain dispatch + pending queues iff this transition flipped
            # the group terminal.  Self-gating; no-op when the group is
            # still active.
            self._terminal_group_resource_releaser.release(group_id)

    def handle_chunk_running(self, job_id: str) -> None:
        """Transition pending → running without touching the progress
        counters.  Used by fleet monitors when the container reaches a
        running state before the worker has had a chance to send its
        first PROGRESS event."""
        self._promote_pending_to_running(job_id)

    def _promote_pending_to_running(self, job_id: str) -> None:
        raw = self._job_repo.get_raw_by_id(job_id)
        if raw is None:
            return
        if raw.get("status") == "pending":
            self._job_repo.update_status(job_id, "running")
            self._job_repo.mark_started(job_id)
            group_id = raw.get("group_id") or ""
            if group_id:
                self.reconcile_group_status(group_id)
                # pending->running can't flip the group terminal, so the
                # release call is always a no-op here.  Kept for the
                # invariant "every reconcile is followed by a release"
                # so future refactors can't reintroduce the leak.
                self._terminal_group_resource_releaser.release(group_id)

    # ------------------------------------------------------------------
    # Read facade — used by monitors instead of repository imports
    # ------------------------------------------------------------------

    def is_job_terminal(self, job_id: str) -> bool:
        raw = self._job_repo.get_raw_by_id(job_id)
        if raw is None:
            return True   # unknown job is treated as terminal so monitors stop
        return str(raw.get("status") or "") in _TERMINAL_GROUP_STATUSES

    def get_job_status(self, job_id: str) -> str | None:
        raw = self._job_repo.get_raw_by_id(job_id)
        return str(raw.get("status") or "") if raw else None

    def get_group_status(self, group_id: str) -> str | None:
        grp = self._group_repo.get_by_id(group_id)
        return str(grp.get("status") or "") if grp else None

    def get_job_raw(self, job_id: str) -> dict[str, Any] | None:
        """Read-only job row.  Monitors use this for fields like
        ``rendered_frames``, ``error``, ``machine_type`` they need per
        tick — instead of importing JobRepository directly."""
        return self._job_repo.get_raw_by_id(job_id)

    # ------------------------------------------------------------------
    # Story 3: user cancelled the render
    # ------------------------------------------------------------------

    def cancel_render(self, group_id: str) -> dict[str, Any]:
        """Group-level cancel.  Delegates the multi-pass teardown to
        ``RenderCanceler`` (which composes ``JobCanceler`` for the
        per-job atom), then writes the terminal snapshot so the list
        endpoint can serve this group without re-fetching its
        children.  Behaviorally identical to the inline implementation
        that lived here before the lifecycle_cancel/ extraction."""
        result = self._render_canceler.cancel(group_id)

        all_jobs = self._job_repo.get_by_group(group_id)
        group = self._group_repo.get_by_id(group_id)
        if group is not None:
            total_frames = group.get("total_frames") or 0
            total_rendered = min(
                total_frames,
                self._output_frames.count_for_group(group_id),
            )
            self._write_terminal_snapshot(group_id, all_jobs, total_rendered)

        return result

    def cancel_one_job(self, job_id: str) -> dict[str, Any]:
        """Per-job cancel (B2 -- the per-instance Cancel button).  Thin
        shim over ``JobTerminator.execute(CANCEL_PIPELINE, ...)``.

        Idempotent: silently no-ops if the job is unknown or already
        terminal.  Otherwise runs the CANCEL_PIPELINE which (in order)
        marks cancelled, releases the machine, releases the ledger,
        stops the monitor, RPCs the provider, attempts a retry on a
        different worker (anti-affinity excludes the cancelled
        machine), drains the fleet's queue, and reconciles the parent
        group.  All step ordering + rules live in
        ``lifecycle_job_termination/pipelines.py``.
        """
        raw = self._job_repo.get_raw_by_id(job_id)
        if raw is None:
            return {"cancelled": False, "reason": "not_found"}
        if str(raw.get("status") or "") in _TERMINAL_GROUP_STATUSES:
            return {"cancelled": False, "reason": "already_terminal"}

        self._fire_modal_terminal_hook_if_modal(raw, job_id)
        exclusions = self._anti_affinity.exclusions_for_chunk(
            raw.get("group_id") or "",
            raw.get("chunk_index") or 0,
            including_row=raw,
        )
        self._terminator.execute(
            pipeline=CANCEL_PIPELINE,
            job_id=job_id,
            raw=raw,
            error="Cancelled by user",
            exclusions=exclusions,
        )
        return {"cancelled": True, "job_id": job_id}

    # ------------------------------------------------------------------
    # Story 4: a job state changed; roll the change up to the group
    # ------------------------------------------------------------------

    def reconcile_group_status(self, group_id: str) -> None:
        """Recompute the group's overall status from its child jobs and
        persist if it changed.  Single owner of group-status mutations
        outside the cancel path — callbacks notify us via this entry
        point instead of touching ``render_groups`` directly.

        On terminal transitions (done/failed) we also write the
        ``terminal_snapshot`` columns so the list endpoint can serve this
        group without re-fetching its children ever again."""
        group = self._group_repo.get_by_id(group_id)
        if not group:
            return
        jobs = self._job_repo.get_by_group(group_id)
        statuses = [j.status for j in jobs]
        total_frames = group["total_frames"] or 0
        total_rendered = min(
            total_frames,
            self._output_frames.count_for_group(group_id),
        )
        # Aggregator needs to know about parked work (no jobs row yet,
        # but live and waiting on the daemon's re-eval) so the group
        # doesn't flip to "failed" while a retry sits in
        # pending_allocation_queue.  Read goes through the orchestrator-
        # side repo; allocation owns writes.
        # In-flight allocation work spans both queues during the
        # pending->dispatch handoff: pending row gets deleted by the
        # pending tick before the dispatch row is consumed into a
        # ``jobs`` row.  Without checking both, an audit-sweep
        # reconcile firing in that window flips the group terminal
        # and TerminalGroupResourceReleaser drains the in-flight row.
        has_pending_allocation = (
            self._pending_allocation_repo.has_pending_for_group(group_id)
            or self._dispatch_allocation_repo.has_dispatch_queued_for_group(group_id)
        )
        result = compute_group_status(
            current_group_status=group["status"],
            job_statuses=statuses,
            total_frames=total_frames,
            total_rendered=total_rendered,
            has_pending_allocation=has_pending_allocation,
        )
        if not result.should_persist:
            return
        self._group_repo.update_status(group_id, result.status)
        if result.status in _TERMINAL_GROUP_STATUSES:
            self._write_terminal_snapshot(group_id, jobs, total_rendered)

    def release_terminal_group_resources(self, group_id: str) -> int:
        """Facade passthrough to ``TerminalGroupResourceReleaser.release``.

        Provided for callers outside ``orchestrator/`` (e.g. the monitor
        lock audit sweep) so the layer rule "outside-orchestrator must
        only touch the lifecycle facade" is preserved.  Self-gating; the
        underlying releaser no-ops when the group isn't terminal.
        """
        return self._terminal_group_resource_releaser.release(group_id)

    def _write_terminal_snapshot(
        self,
        group_id: str,
        jobs: list[RenderJob],
        total_rendered: int,
    ) -> None:
        """Snapshot the per-group fields that the list view needs.  All
        frame-related fields come from the ``output_frames`` table —
        single SQL query per field, no per-job parsing."""
        latest = self._output_frames.latest_for_group(group_id)
        latest_file = latest[0] if latest else None
        latest_job_id = latest[1] if latest else None
        available = self._output_frames.count_for_group(group_id)
        self._group_repo.update_terminal_snapshot(
            group_id,
            tasks_count=len(jobs),
            latest_output_file=latest_file,
            latest_output_job_id=latest_job_id,
            available_output_files_count=available,
            overall_rendered_frames=total_rendered,
        )

    # ------------------------------------------------------------------
    # Telemetry — single render_telemetry row per successful chunk.
    # Skipped silently for community jobs and any chunk missing the
    # price-per-hour stamp from dispatch (legacy rows, pre-Phase-5).
    # ------------------------------------------------------------------

    def _record_telemetry(
        self, job_id: str, group_id: str, raw_pre_done: dict[str, Any],
    ) -> None:
        from datetime import datetime, timezone
        price = raw_pre_done.get("price_per_hour_at_dispatch")
        started_at = raw_pre_done.get("started_at")
        if price is None or started_at is None:
            log.info(
                "Skipping telemetry for job %s — missing %s",
                job_id,
                "price_per_hour_at_dispatch" if price is None else "started_at",
            )
            return

        post_done = self._job_repo.get_raw_by_id(job_id) or raw_pre_done
        completed_at = post_done.get("completed_at") or datetime.now(timezone.utc).isoformat()
        seconds_total = _seconds_between(started_at, completed_at)
        chunk_size = _chunk_size_from_row(post_done)
        rendered_frames = int(post_done.get("rendered_frames") or 0)
        price_per_hour = float(price)
        cost_actual = (seconds_total / 3600.0) * price_per_hour

        heaviness = self._fetch_heaviness(group_id) if group_id else {}
        file_size_bytes = self._fetch_file_size(group_id) if group_id else None

        self._telemetry.record_chunk(
            job_id=job_id,
            group_id=group_id,
            fleet=str(post_done.get("machine_type") or ""),
            gpu_type=post_done.get("gpu_type"),
            machine_id=post_done.get("machine_id"),
            chunk_size=chunk_size,
            rendered_frames=rendered_frames,
            started_at=_iso_str(started_at),
            completed_at=_iso_str(completed_at),
            seconds_total=seconds_total,
            price_per_hour=price_per_hour,
            cost_actual_usd=cost_actual,
            heaviness=heaviness,
            file_size_bytes=file_size_bytes,
            seconds_estimated=None,
            cost_estimated_usd=None,
        )

    def _fetch_heaviness(self, group_id: str) -> dict[str, Any]:
        try:
            group = self._group_repo.get_by_id(group_id)
            if not group:
                return {}
            raw = group.get("resolved_scene_json")
            if not raw:
                return {}
            parsed = json.loads(raw)
            heaviness = parsed.get("heaviness") if isinstance(parsed, dict) else None
            return heaviness if isinstance(heaviness, dict) else {}
        except Exception:
            return {}

    def _fetch_file_size(self, group_id: str) -> int | None:
        try:
            group = self._group_repo.get_by_id(group_id)
            if not group:
                return None
            value = group.get("r2_input_size_bytes")
            return int(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _engine_from_overrides_json(raw: str | None) -> str | None:
        """Extract ``render.engine`` from a render-overrides JSON string.
        Returns None on any parse error or missing value."""
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        render_section = parsed.get("render")
        if not isinstance(render_section, dict):
            return None
        engine = render_section.get("engine")
        return engine if isinstance(engine, str) and engine else None


# ---------------------------------------------------------------------------
# Telemetry helpers (module-level — pure functions, no class state)
# ---------------------------------------------------------------------------

def _iso_str(value: Any) -> str:
    from datetime import datetime
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _seconds_between(start: Any, end: Any) -> int:
    try:
        s = _to_dt(start)
        e = _to_dt(end)
        if s is None or e is None:
            return 0
        return max(0, int((e - s).total_seconds()))
    except Exception:
        return 0


def _to_dt(value: Any):
    from datetime import datetime
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _chunk_size_from_row(row: dict[str, Any]) -> int:
    start = int(row.get("frame_start") or 0)
    end = int(row.get("frame_end") or 0)
    step = max(1, int(row.get("frame_step") or 1))
    if end < start:
        return 0
    return ((end - start) // step) + 1
