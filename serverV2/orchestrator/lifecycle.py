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

import base64
import json
import logging
from typing import Any, Callable

from serverV2.callbacks.group_status_aggregator import compute_group_status
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
from serverV2.repositories.dispatch_queue_repository import DispatchQueueRepository
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)

_TERMINAL_GROUP_STATUSES = frozenset({"done", "failed", "cancelled"})


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
        coordinator: DispatchCoordinator,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        queue_repo: DispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        fleet_registry: FleetRegistry,
        resource_picker: Callable[[], AvailableResources],
    ) -> None:
        self._default_strategy = default_strategy
        self._fast_render_strategy = fast_render_strategy
        self._coordinator = coordinator
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._fleet = fleet_registry
        self._resource_picker = resource_picker

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
    ) -> list[PlannedTask]:
        # File size for strategy selection is carried inside ``heaviness``
        # (see ``parse_analysis_heaviness(snapshot, file_size_bytes=...)``).
        # ``heaviness=None`` is fine — equivalent to a defaulted dict with
        # file_size=0; the picker treats unknown size as "use Default".
        file_size_bytes = int((heaviness or {}).get("file_size_bytes", 0) or 0)

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
        strategy = self._pick_strategy(file_size_bytes, total_frames)
        tasks = strategy.allocate_initial(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            engine=engine,
            heaviness=heaviness,
        )
        log.info(
            "plan: strategy=%s pinned=%s engine=%s raw_community=%d raw_caps=%s "
            "after_filter_community=%d after_filter_caps=%d in_flight=%s "
            "total_frames=%d file_size_bytes=%s -> tasks=%d",
            type(strategy).__name__, pinned, engine, raw_community, raw_caps_by_fleet,
            len(resources.community_machines), len(resources.serverless_capabilities),
            dict(resources.serverless_in_flight), total_frames, file_size_bytes,
            len(tasks),
        )
        return tasks

    def _pick_strategy(
        self, file_size_bytes: int, total_frames: int,
    ) -> FrameAllocator:
        size_known = file_size_bytes > 0
        is_heavy = size_known and file_size_bytes >= self._FAST_RENDER_FILE_SIZE_BYTES
        is_long = total_frames >= self._FAST_RENDER_TOTAL_FRAMES
        if is_heavy or is_long:
            return self._fast_render_strategy
        return self._default_strategy

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
        engine = self._engine_from_overrides_json(render_overrides_json)
        context = DispatchContext(
            group_id=group_id,
            input_filename=input_filename,
            render_overrides_b64=overrides_b64,
            blend_url="",
            max_retries=MAX_RETRIES,
            priority=0,
            engine=engine,
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

        # Anti-affinity: don't retry on the same fleet/gpu_type or
        # community machine that just failed.
        excluded_caps, excluded_ids = self._exclusions_for(raw)

        # Load heaviness signal + engine from the group so the retry strategy
        # can size + filter the same way as initial allocation.
        file_size_bytes: int | None = None
        engine: str | None = None
        if grp is not None:
            raw_size = grp.get("r2_input_size_bytes")
            if raw_size is not None:
                try:
                    file_size_bytes = int(raw_size)
                except (TypeError, ValueError):
                    file_size_bytes = None
            raw_overrides = grp.get("render_overrides_json")
            if raw_overrides:
                try:
                    parsed = json.loads(raw_overrides)
                    if isinstance(parsed, dict):
                        render_section = parsed.get("render")
                        if isinstance(render_section, dict):
                            engine_value = render_section.get("engine")
                            if isinstance(engine_value, str):
                                engine = engine_value
                except (TypeError, ValueError, json.JSONDecodeError):
                    engine = None

        chunk_request = ChunkRequest(
            group_id=group_id,
            chunk_index=chunk_index,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=rj.frame_step,
            total_frames=total_frames,
            attempt=next_attempt,
            excluded_machine_ids=excluded_ids,
            excluded_serverless_capabilities=excluded_caps,
            file_size_bytes=file_size_bytes,
            engine=engine,
        )
        retry_strategy = self._pick_strategy(file_size_bytes, total_frames)
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
            engine=engine,
        )
        self._coordinator.enqueue_and_flush(group_id, [retry_task], context)
        return True

    # ------------------------------------------------------------------
    # Story 3: user cancelled the render
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
                sum(j.rendered_frames for j in all_jobs),
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
            sum(j.rendered_frames for j in jobs),
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
                latest_job_id = j.id
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
