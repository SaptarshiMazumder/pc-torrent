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

    FrameAllocator       — picks fleet targets for chunks (initial + retry)
    DispatchCoordinator  — drains the queue, claims the ledger, dispatches
    GroupStatusAggregator (pure function) — computes group-level status

Open THIS file to understand what happens during a render.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from serverV2.callbacks.group_status_aggregator import compute_group_status
from serverV2.orchestrator.allocation import tiers
from serverV2.orchestrator.allocation.analyzers.cost_analyzer import (
    MixSlot,
    estimate_cost_for_mix,
)
from serverV2.core.models import (
    AvailableResources,
    DispatchContext,
    DispatchResult,
    PlannedTask,
    RenderJob,
)
from serverV2.core.value_objects import (
    latest_output_filename,
    output_frame_sort_key,
    parse_output_files,
)
from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest
from serverV2.orchestrator.allocation.frame_allocator import FrameAllocator
from serverV2.orchestrator.config import MAX_RETRIES
from serverV2.orchestrator.dispatch.coordinator import DispatchCoordinator
from serverV2.orchestrator.lifecycle_output import LifecycleFramesDeduplication
from serverV2.repositories.dispatch_queue_repository import DispatchQueueRepository
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository
from serverV2.repositories.telemetry_repository import TelemetryRepository

log = logging.getLogger(__name__)

_TERMINAL_GROUP_STATUSES = frozenset({"done", "failed", "cancelled"})


class ManualRetryError(Exception):
    """User-triggered retry refused.  ``reason`` is a short stable code the
    router maps to an HTTP status — see ``RETRY_REASON_HTTP_STATUS`` below."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Stable reason codes for ``ManualRetryError``.  Kept here next to the
# raise sites so the router translation stays in sync.
RETRY_REASON_HTTP_STATUS: dict[str, int] = {
    "not_found": 404,
    "not_failed": 409,
    "active_sibling_exists": 409,
    "group_cancelled": 409,
    "no_remaining_frames": 409,
    "no_eligible_target": 503,
}


class RenderLifecycle:

    # Heuristic thresholds for picking the FastRender strategy over the
    # Default one.  Either condition (heavy file OR many frames) is enough.
    _FAST_RENDER_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB
    _FAST_RENDER_TOTAL_FRAMES = 30

    def __init__(
        self,
        *,
        default_strategy: FrameAllocator,
        fast_render_strategy: FrameAllocator,
        economy_strategy: FrameAllocator,
        coordinator: DispatchCoordinator,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        machine_repo: MachineRepository,
        queue_repo: DispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        telemetry_repo: TelemetryRepository,
        fleet_registry: FleetRegistry,
        resource_picker: Callable[[], AvailableResources],
    ) -> None:
        self._default_strategy = default_strategy
        self._fast_render_strategy = fast_render_strategy
        self._economy_strategy = economy_strategy
        self._coordinator = coordinator
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._machine_repo = machine_repo
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._telemetry = telemetry_repo
        self._fleet = fleet_registry
        self._resource_picker = resource_picker
        # Output-layer helper: authoritative rendered-frame accounting via
        # set-union of output_files across sibling attempts.  Preparation
        # for the broader RenderLifecycle SRP refactor -- the lifecycle's
        # job is narrative orchestration, not data-shape computation.
        self._dedup = LifecycleFramesDeduplication(job_repo=job_repo)

    # ------------------------------------------------------------------
    # Planning — split frames across fleet targets (no dispatch)
    # ------------------------------------------------------------------

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
        # File size for strategy selection is carried inside ``heaviness``
        # (see ``parse_analysis_heaviness(snapshot, file_size_bytes=...)``).
        # ``heaviness=None`` is fine — equivalent to a defaulted dict with
        # file_size=0; the picker treats unknown size as "use Default".
        file_size_bytes = int((heaviness or {}).get("file_size_bytes", 0) or 0)

        resolved_tier = tiers.normalize(tier)

        resources = self._resource_picker()
        raw_community = len(resources.community_machines)
        raw_caps_by_fleet: dict[str, int] = {}
        for cap in resources.serverless_capabilities:
            raw_caps_by_fleet[cap.fleet] = raw_caps_by_fleet.get(cap.fleet, 0) + 1
        pinned = bool(machine_ids)
        if pinned:
            # User pinned specific community machines.  Filter the pool
            # accordingly and drop serverless capabilities entirely.
            wanted = set(machine_ids)
            resources = AvailableResources(
                community_machines=[
                    m for m in resources.community_machines if m.id in wanted
                ],
                serverless_capabilities=[],
                serverless_in_flight=resources.serverless_in_flight,
            )
        strategy = self._pick_strategy(resolved_tier, file_size_bytes, total_frames)
        tier_budget = self._tier_budget(resolved_tier, heaviness, total_frames)
        tasks = strategy.allocate_initial(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
            tier_budget_usd=tier_budget,
        )
        log.info(
            "plan: tier=%s strategy=%s pinned=%s engine=%s raw_community=%d raw_caps=%s "
            "after_filter_community=%d after_filter_caps=%d in_flight=%s "
            "total_frames=%d file_size_bytes=%s tier_budget=%s -> tasks=%d",
            resolved_tier, type(strategy).__name__, pinned, engine, raw_community,
            raw_caps_by_fleet, len(resources.community_machines),
            len(resources.serverless_capabilities), dict(resources.serverless_in_flight),
            total_frames, file_size_bytes, tier_budget, len(tasks),
        )
        return tasks

    def _pick_strategy(
        self, tier: str, file_size_bytes: int, total_frames: int,
    ) -> FrameAllocator:
        """Tier-first strategy routing.  Falls back to the heaviness
        heuristic for STANDARD (Default vs FastRender)."""
        if tier == tiers.ECONOMY:
            return self._economy_strategy
        if tier == tiers.PREMIUM:
            # Reserved — UI disables the picker.  If a Premium request
            # somehow arrives, fall through to STANDARD's selection.
            log.warning("plan: PREMIUM tier requested but not implemented; falling back to STANDARD")
        # STANDARD (or unknown / fallback): heaviness heuristic decides
        # between Default and FastRender (today's behaviour).
        size_known = file_size_bytes > 0
        is_heavy = size_known and file_size_bytes >= self._FAST_RENDER_FILE_SIZE_BYTES
        is_long = total_frames >= self._FAST_RENDER_TOTAL_FRAMES
        if is_heavy or is_long:
            return self._fast_render_strategy
        return self._default_strategy

    def _tier_budget(
        self, tier: str, heaviness: dict | None, total_frames: int,
    ) -> float | None:
        """Per-tier soft budget cap, in USD.

        Derived from "what would a single A6000 cost to render this
        scene" — scales with scene weight automatically.  None disables
        the cap (Economy doesn't need one; Premium would also be None).
        """
        if tier != tiers.STANDARD or heaviness is None or total_frames <= 0:
            return None
        # A6000 reference: speed=1.30, price=$0.55/hr (matches config.json).
        # Standard's cap = 1.0x A6000-equivalent cost.  With FastRender's
        # internal BUDGET_CAP_MULTIPLIER=1.5, total headroom is 1.5x.
        slot = MixSlot(render_speed=1.30, price_per_hour=0.55, frames_assigned=total_frames)
        try:
            est = estimate_cost_for_mix(heaviness, [slot])
        except Exception:
            return None
        return est.cost_mid_usd if est.cost_mid_usd > 0 else None

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

        engine = self._engine_from_overrides_json(render_overrides_json)
        context = DispatchContext(
            group_id=group_id,
            input_filename=input_filename,
            render_overrides_json=render_overrides_json,
            blend_url="",
            max_retries=MAX_RETRIES,
            priority=0,
            engine=engine,
        )
        return self._coordinator.enqueue_and_flush(group_id, tasks, context)

    # ------------------------------------------------------------------
    # Story 2: a chunk failed (full flow — retry decision + DB mutations)
    # ------------------------------------------------------------------

    def handle_chunk_failed(self, job_id: str, error: str) -> None:
        """Single entry point for chunk-failure routing.  Decides whether
        to retry, marks the original job failed, and drains the failed
        fleet's queue (a slot just opened up regardless).  On exhausted
        retries, also rolls the new state up to the parent group.

        Adapters (callbacks, monitors) call ``RenderOrchestrator.on_job_failed``
        which delegates here — they never touch repositories themselves.
        """
        # Capture failed fleet BEFORE retry — the retry may dispatch on a
        # different fleet (anti-affinity), but the slot we freed is in this
        # job's fleet.
        raw = self._job_repo.get_raw_by_id(job_id)
        failed_fleet = (raw.get("machine_type") or "") if raw else ""

        retried = self._try_dispatch_retry(job_id, error)
        # Mark failed AFTER the retry attempt so the group always has at
        # least one active job during the transition (prevents premature
        # group-failed status flicker in the UI).
        self._job_repo.mark_failed(job_id, error)

        # Release the community machine lock so the allocator can return
        # this PC for the next dispatch (or the just-queued retry, if it
        # landed on this PC).  Vast/Modal have no machines row.
        machine_id = (raw.get("machine_id") if raw else None)
        if failed_fleet == "windows" and machine_id:
            self._machine_repo.set_available(machine_id)

        if retried:
            log.info("Job %s failed but retry dispatched: %s", job_id, error)
        else:
            # Generic message — the actual reason was logged inside
            # _try_dispatch_retry (max retries hit / no eligible target /
            # group cancelled / chunk already complete).  Don't mislabel
            # them all as "retries exhausted" here.
            log.warning("Job %s permanently failed (no retry dispatched): %s", job_id, error)
            group_id = (raw.get("group_id") or "") if raw else ""
            if group_id:
                self.reconcile_group_status(group_id)

        if failed_fleet:
            try:
                self._coordinator.drain_for_fleet(failed_fleet)
            except Exception as exc:
                log.warning("drain_for_fleet(%s) failed: %s", failed_fleet, exc)

    def _try_dispatch_retry(self, job_id: str, error: str) -> bool:
        """Returns True if a retry was dispatched, False if the chunk is
        giving up.  Internal — callers go through ``handle_chunk_failed``."""
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

        # Compute remaining frames from the union of ALL sibling attempts'
        # outputs, not just this failing job's.  Defends against the
        # auto-retry / manual-retry / duplicate-dispatch edge cases where
        # earlier or parallel attempts already rendered some frames in
        # this chunk's range.  See _compute_dedup_remaining_for_chunk.
        dedup_remaining = self._dedup.compute_remaining_for_chunk(
            group_id, chunk_index,
        )
        if dedup_remaining is None:
            self._in_progress.release(group_id, chunk_index)
            return False

        next_attempt = (rj.attempt or 0) + 1
        if next_attempt > MAX_RETRIES:
            log.warning(
                "Job %s: max retries (%d) exhausted for frames %d-%d",
                job_id, MAX_RETRIES, dedup_remaining[0], dedup_remaining[1],
            )
            self._in_progress.release(group_id, chunk_index)
            return False

        frame_start, frame_end, frame_step = dedup_remaining
        total_frames = ((frame_end - frame_start) // frame_step) + 1

        # Anti-affinity: don't retry on the same fleet/gpu_type or
        # community machine that just failed.
        excluded_caps, excluded_ids = self._exclusions_for(raw)

        file_size_bytes, engine, tier = self._load_group_dispatch_context(grp)

        chunk_request = ChunkRequest(
            group_id=group_id,
            chunk_index=chunk_index,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            attempt=next_attempt,
            excluded_machine_ids=excluded_ids,
            excluded_serverless_capabilities=excluded_caps,
            file_size_bytes=file_size_bytes,
            engine=engine,
        )
        retry_strategy = self._pick_strategy(
            tiers.normalize(tier), file_size_bytes, total_frames,
        )
        retry_task = retry_strategy.allocate_retry(chunk_request, self._resource_picker())
        if retry_task is None:
            log.error(
                "Job %s: no eligible target for retry of frames %d-%d (group %s)",
                job_id, frame_start, frame_end, group_id,
            )
            return False

        target_label = (
            f"{retry_task.fleet}/{retry_task.gpu_type}"
            if retry_task.gpu_type
            else f"{retry_task.fleet}/{retry_task.machine_id}"
        )
        log.info(
            "Job %s: requeued frames %d-%d (attempt %d/%d) on %s",
            job_id, frame_start, frame_end, next_attempt, MAX_RETRIES, target_label,
        )

        if not rj.render_overrides_json:
            raise RuntimeError(
                f"job {rj.id} has no render_overrides_json — cannot retry"
            )
        context = DispatchContext(
            group_id=group_id,
            input_filename=rj.input_filename,
            render_overrides_json=rj.render_overrides_json,
            blend_url="",
            max_retries=rj.max_retries,
            priority=rj.priority,
            engine=engine,
        )
        self._coordinator.enqueue_and_flush(group_id, [retry_task], context)
        return True

    # ------------------------------------------------------------------
    # Story 2c1: community machine reports idle (post-register / post-job)
    # ------------------------------------------------------------------

    def handle_community_machine_idle(self, machine_id: str) -> None:
        """A community machine just signalled "I'm idle, ready for work"
        (via ``PUT /machines/{id}/available``).  Going-idle implies the
        agent has nothing in flight, so any ``status='running'`` jobs
        still tied to this machine are abandoned — the agent restarted
        across a sidecar rebuild, OOM, network blip, etc., losing local
        state — and need to be marked failed so the standard retry path
        can pick them up.

        Vast/Modal don't need this: their per-job monitors observe
        provider-side container death directly.  Community has no
        per-job monitor; the sidecar restart is otherwise invisible
        until heartbeat staleness triggers (which doesn't fire if the
        agent comes back online quickly).
        """
        if not machine_id:
            return
        abandoned = self._job_repo.get_running_for_machine(machine_id)
        for row in abandoned:
            job_id = row.get("id")
            if not job_id:
                continue
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
        """User pressed "Retry" on a stuck chunk in the UI.  Resolves the
        latest attempt for the chunk, validates it's actually stuck (failed
        + auto-retries exhausted + nothing active), and dispatches a fresh
        attempt with ``attempt=0`` for the un-uploaded frames only.

        Anti-affinity excludes the latest failed target so we don't retry
        on the same fleet/GPU that just gave up.

        Group status flips terminal → running automatically via
        ``reconcile_group_status`` once the new pending job appears.

        Raises ``ManualRetryError`` with a stable reason code on refusal.
        """
        raw = self._job_repo.get_raw_by_id(job_id)
        if not raw:
            raise ManualRetryError("not_found")
        group_id = raw.get("group_id") or ""
        if not group_id:
            raise ManualRetryError("not_found")
        chunk_index = raw.get("chunk_index") or 0

        grp = self._group_repo.get_by_id(group_id)
        if grp and grp.get("status") == "cancelled":
            raise ManualRetryError("group_cancelled")

        # Already-active retry for this chunk — don't double-fire.
        if self._in_progress.current_job_for(group_id, chunk_index) is not None:
            raise ManualRetryError("active_sibling_exists")

        # Resolve the latest attempt for this chunk.  Caller may have passed
        # an older attempt's job_id; we always operate on the latest for
        # status/anti-affinity decisions.  Frame-range computation goes
        # through the dedup helper instead, which considers the union of
        # outputs across ALL sibling attempts (handles duplicate-dispatch
        # races and partial-progress retry chains correctly).
        siblings = [
            j for j in self._job_repo.get_raw_by_group(group_id)
            if (j.get("chunk_index") or 0) == chunk_index
        ]
        if not siblings:
            raise ManualRetryError("not_found")
        latest = max(
            siblings,
            key=lambda j: (j.get("attempt") or 0, j.get("submitted_at") or ""),
        )
        if (latest.get("status") or "") != "failed":
            raise ManualRetryError("not_failed")

        rj = RenderJob.from_row(latest)
        dedup_remaining = self._dedup.compute_remaining_for_chunk(
            group_id, chunk_index,
        )
        if dedup_remaining is None:
            raise ManualRetryError("no_remaining_frames")

        frame_start, frame_end, frame_step = dedup_remaining
        total_frames = ((frame_end - frame_start) // frame_step) + 1

        # Anti-affinity from the latest failed attempt (per user spec).
        excluded_caps, excluded_ids = self._exclusions_for(latest)

        file_size_bytes, engine, tier = self._load_group_dispatch_context(grp)

        chunk_request = ChunkRequest(
            group_id=group_id,
            chunk_index=chunk_index,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            attempt=0,
            excluded_machine_ids=excluded_ids,
            excluded_serverless_capabilities=excluded_caps,
            file_size_bytes=file_size_bytes,
            engine=engine,
        )
        retry_strategy = self._pick_strategy(
            tiers.normalize(tier), file_size_bytes or 0, total_frames,
        )
        retry_task = retry_strategy.allocate_retry(chunk_request, self._resource_picker())
        if retry_task is None:
            raise ManualRetryError("no_eligible_target")

        target_label = (
            f"{retry_task.fleet}/{retry_task.gpu_type}"
            if retry_task.gpu_type
            else f"{retry_task.fleet}/{retry_task.machine_id}"
        )
        log.info(
            "Manual retry for chunk %d (group %s): frames %d-%d on %s "
            "(attempt reset to 0, latest failed attempt was %d)",
            chunk_index, group_id, frame_start, frame_end, target_label,
            rj.attempt or 0,
        )

        if not rj.render_overrides_json:
            raise RuntimeError(
                f"job {rj.id} has no render_overrides_json — cannot manually retry"
            )
        context = DispatchContext(
            group_id=group_id,
            input_filename=rj.input_filename,
            render_overrides_json=rj.render_overrides_json,
            blend_url="",
            max_retries=rj.max_retries,
            priority=rj.priority,
            engine=engine,
        )
        results = self._coordinator.enqueue_and_flush(group_id, [retry_task], context)

        # Aggregator sees the new pending job and unlocks the group from
        # any terminal state (failed) back to running.
        self.reconcile_group_status(group_id)

        new_job_id = results[0].job_id if results else None
        return {
            "new_job_id": new_job_id,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "fleet": retry_task.fleet,
            "gpu_type": retry_task.gpu_type,
        }

    def _load_group_dispatch_context(
        self, grp: dict[str, Any] | None,
    ) -> tuple[int | None, str | None, str | None]:
        """Pull (file_size_bytes, engine, tier) off a render_groups row for
        the dispatch path.  Used by both auto-retry and manual-retry."""
        file_size_bytes: int | None = None
        engine: str | None = None
        tier: str | None = None
        if grp is None:
            return file_size_bytes, engine, tier

        raw_size = grp.get("r2_input_size_bytes")
        if raw_size is not None:
            try:
                file_size_bytes = int(raw_size)
            except (TypeError, ValueError):
                file_size_bytes = None

        raw_overrides = grp.get("render_overrides_json")
        if raw_overrides:
            parsed = json.loads(raw_overrides)
            if isinstance(parsed, dict):
                render_section = parsed.get("render")
                if isinstance(render_section, dict):
                    engine_value = render_section.get("engine")
                    if isinstance(engine_value, str):
                        engine = engine_value

        tier_raw = grp.get("tier")
        if isinstance(tier_raw, str):
            tier = tier_raw

        return file_size_bytes, engine, tier

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
        # the window from up-to-poll-interval seconds to milliseconds.
        # Best-effort — try/except + the monitor stays as the safety net.
        # Community has no provider call (provider_job_id is empty), so the
        # short-circuit no-ops; the agent's cancel-status poll handles the
        # community equivalent.
        try:
            provider_job_id = (
                raw.get("runpod_job_id")
                or raw.get("modal_function_call_id")
                or ""
            )
            if fleet and provider_job_id:
                strategy = self._fleet.get(fleet)
                if strategy is not None:
                    strategy.cancel(provider_job_id)
                    strategy.stop_monitoring(job_id)
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
            self._machine_repo.set_available(machine_id)

        try:
            self._record_telemetry(job_id, group_id, raw)
        except Exception as exc:
            # Telemetry must never break the success path.
            log.warning("Telemetry write failed for job %s: %s", job_id, exc)

        if group_id:
            self.reconcile_group_status(group_id)

        if fleet:
            try:
                self._coordinator.drain_for_fleet(fleet)
            except Exception as exc:
                log.warning("drain_for_fleet(%s) failed: %s", fleet, exc)

    # ------------------------------------------------------------------
    # Story 2c: a chunk reported progress (frame uploaded)
    # ------------------------------------------------------------------

    def handle_chunk_progress(
        self, job_id: str, rendered_frames: int, total_frames: int,
    ) -> None:
        """Update progress counters; on the first PROGRESS event, transition
        pending → running, stamp ``started_at`` (telemetry uses it), and
        roll the change up to the parent group."""
        self._job_repo.update_progress(job_id, rendered_frames, total_frames)
        self._promote_pending_to_running(job_id)

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
        """Mark cancelled → stop monitors → drain queue + ledger → cancel
        provider-side jobs.  Order matters: marking the group cancelled
        first blocks any in-flight requeues via the DB guard."""
        # 1. Mark group + active jobs cancelled in the DB.  Also release
        # any community machine locks so the freed PCs become available
        # for new dispatches immediately (auto-demotion would catch them
        # eventually via heartbeat-stale, but this is faster and clean).
        self._group_repo.update_status(group_id, "cancelled")
        jobs = self._job_repo.get_active_by_group(group_id)
        for job in jobs:
            self._job_repo.update_status(job["id"], "cancelled", error="Cancelled by user")
            machine_id = job.get("machine_id")
            if (job.get("machine_type") or "") == "windows" and machine_id:
                self._machine_repo.set_available(machine_id)

        # 2. Stop monitor threads so they stop acting on this group
        for job in jobs:
            fleet = job.get("machine_type") or ""
            strategy = self._fleet.get(fleet)
            if strategy:
                strategy.stop_monitoring(job["id"])

        # 3. Drain the dispatch queue + in-progress ledger for the group
        drained = self._queue_repo.drain(group_id)
        if drained:
            log.info("Group %s: drained %d items from dispatch queue", group_id, drained)
        self._in_progress.release_all(group_id)

        # 4. Cancel provider-side jobs (Modal function calls, Vast instances)
        for job in jobs:
            job_id = job["id"]
            fleet = job.get("machine_type") or ""
            strategy = self._fleet.get(fleet)
            if not strategy:
                log.warning(
                    "cancel %s: no strategy for fleet=%r — provider job not cancelled",
                    job_id, fleet,
                )
                continue
            if not strategy.is_enabled():
                log.warning(
                    "cancel %s: fleet %s disabled — provider job not cancelled",
                    job_id, fleet,
                )
                continue
            pid = strategy.provider_job_id_from_job(job)
            if not pid:
                log.warning(
                    "cancel %s: fleet=%s has no provider_job_id stored — cannot cancel provider-side",
                    job_id, fleet,
                )
                continue
            try:
                strategy.cancel(pid)
                log.info("cancel %s: fleet=%s pid=%s — cancel call returned", job_id, fleet, pid)
            except Exception as exc:
                log.warning(
                    "cancel %s: fleet=%s pid=%s raised %s: %s",
                    job_id, fleet, pid, type(exc).__name__, exc,
                )

        log.info("Group %s: cancelled %d jobs", group_id, len(jobs))

        # Snapshot the per-group fields the list view will read for this
        # cancelled group, so the list endpoint never needs to re-fetch
        # children for it again.
        all_jobs = self._job_repo.get_by_group(group_id)
        group = self._group_repo.get_by_id(group_id)
        if group is not None:
            total_frames = group.get("total_frames") or 0
            total_rendered = min(
                total_frames,
                self._dedup.compute_total_rendered(all_jobs),
            )
            self._write_terminal_snapshot(group_id, all_jobs, total_rendered)

        return {"cancelled_jobs": len(jobs)}

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
            self._dedup.compute_total_rendered(jobs),
        )
        result = compute_group_status(
            current_group_status=group["status"],
            job_statuses=statuses,
            total_frames=total_frames,
            total_rendered=total_rendered,
        )
        if not result.should_persist:
            return
        self._group_repo.update_status(group_id, result.status)
        if result.status in _TERMINAL_GROUP_STATUSES:
            self._write_terminal_snapshot(group_id, jobs, total_rendered)

    def _write_terminal_snapshot(
        self,
        group_id: str,
        jobs: list[RenderJob],
        total_rendered: int,
    ) -> None:
        """Snapshot the per-group fields that the list view needs.  Picked
        from the children once, persisted to ``render_groups``.  See
        ``RenderGroupRepository.update_terminal_snapshot``."""
        latest_file: str | None = None
        latest_job_id: str | None = None
        latest_key: tuple[int, str] | None = None
        available = 0
        for j in jobs:
            files = parse_output_files(j.output_files)
            available += len(files)
            top = latest_output_filename(files)
            if not top:
                continue
            key = output_frame_sort_key(top)
            if latest_key is None or key > latest_key:
                latest_file = top
                latest_job_id = j.job_id
                latest_key = key
        self._group_repo.update_terminal_snapshot(
            group_id,
            tasks_count=len(jobs),
            latest_output_file=latest_file,
            latest_output_job_id=latest_job_id,
            available_output_files_count=available,
            overall_rendered_frames=total_rendered,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _exclusions_for(
        job_row: dict[str, Any],
    ) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        """Return ``(excluded_serverless_capabilities, excluded_machine_ids)``
        for anti-affinity on retry."""
        fleet = (job_row.get("machine_type") or "").strip()
        gpu_type = (job_row.get("gpu_type") or "").strip()
        machine_id = (job_row.get("machine_id") or "").strip()
        if fleet in ("modal_serverless", "vast_serverless") and gpu_type:
            return ((fleet, gpu_type),), ()
        if machine_id:
            return (), (machine_id,)
        return (), ()

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
            raw = group.get("analysis_snapshot_json")
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
