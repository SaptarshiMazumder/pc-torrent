"""RenderGroupTelemetryService — owns the active render-group DTO + its Redis
mirror.

Why this exists: the active-group DTO is read on a 3s frontend poll but only
*changes* on lifecycle events.  So we build it AT EVENTS (async, off the render
path) into Redis via ``RenderGroupRedisMirror``, and the read endpoints serve
from the mirror (Postgres fallback on miss / Redis down).

  * refresh_live(gid)  -- FIRE-AND-FORGET: submits build+write to a background
    executor and returns immediately, so ``reconcile_group_status`` /
    ``register_outputs`` are never blocked by the ~7-query build.  Self-decides
    write (active) vs remove (terminal) from the group's current status.
  * get_live / get_active_map -- the read path (mirror, Postgres fallback).

The ``build`` logic (formerly ``RenderGroupService._build_active_status_dto``)
lives here because this service owns the active DTO; RenderGroupService keeps
the terminal DTO and delegates the active path here (one-way, no cycle).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from serverV2.config import usd_to_credits
from serverV2.core.value_objects import parse_json_object
from serverV2.allocation.allocation_strategies.allocation_helpers import allocation_tiers as tiers
from serverV2.orchestrator.chunk_progress import ChunkProgress, ChunkProgressService
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.services.pre_render import SceneResolver
from serverV2.services.render_groups.serializers import RenderGroupSerializer
from serverV2.services.render_groups.telemetry.render_group_redis_mirror import (
    RenderGroupRedisMirror,
)

log = logging.getLogger(__name__)

_ACTIVE_GROUP_STATUSES = frozenset({"uploading", "pending", "running"})


class RenderGroupTelemetryService:

    def __init__(
        self,
        *,
        mirror: RenderGroupRedisMirror,
        group_repo,
        job_repo,
        machine_repo,
        output_frame_repo: OutputFrameRepository,
        chunk_progress: ChunkProgressService,
        scene_resolver: SceneResolver,
        get_max_retries: Callable[[], int],
        get_credits_per_usd: Callable[[], float],
        actual_cost_compute,
        max_workers: int = 4,
    ) -> None:
        self._mirror = mirror
        self._groups = group_repo
        self._jobs = job_repo
        self._machines = machine_repo
        self._output_frames = output_frame_repo
        self._chunk_progress = chunk_progress
        self._scene_resolver = scene_resolver
        self._get_max_retries = get_max_retries
        self._get_credits_per_usd = get_credits_per_usd
        self._serializer = RenderGroupSerializer(
            output_frame_repo=output_frame_repo,
            actual_cost_compute=actual_cost_compute,
            get_credits_per_usd=get_credits_per_usd,
        )
        # Small pool so an event burst can't fan out unboundedly; the build
        # is ~7 indexed queries, not CPU-heavy, so a few threads is plenty.
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="rg-telemetry",
        )

    # ------------------------------------------------------------------
    # WRITE -- fire-and-forget, off the render path
    # ------------------------------------------------------------------

    def refresh_live(self, group_id: str) -> None:
        """Append-only hook fired at lifecycle events.  Returns immediately;
        the build + mirror write run on the background executor so the render
        path keeps its latency.  Best-effort: a failed build just leaves the
        last mirror value, and reads fall back to Postgres."""
        if not group_id:
            return
        try:
            self._executor.submit(self._safe_refresh, group_id)
        except RuntimeError:
            # Executor shut down (process teardown) -- drop the refresh.
            pass

    def _safe_refresh(self, group_id: str) -> None:
        try:
            group = self._groups.get_by_id(group_id)
            if not group:
                return
            uid = group.get("user_id")
            if not uid:
                return
            if group.get("status") in _ACTIVE_GROUP_STATUSES:
                self._mirror.write(uid, group_id, self.build_for_group(group))
            else:
                # Terminal / gone -> drop from the active mirror.
                self._mirror.remove(uid, group_id)
        except Exception as exc:
            log.warning("Telemetry refresh failed for group %s: %s", group_id, exc)

    # ------------------------------------------------------------------
    # READ -- mirror first, Postgres fallback
    # ------------------------------------------------------------------

    def get_live(self, user_id: str, group_id: str, group: dict | None = None) -> dict | None:
        """Active DTO for one group.  Mirror hit -> return it; miss -> build
        from Postgres, populate the mirror, return.  ``group`` may be passed
        pre-loaded (list path) to skip a re-read on the build fallback."""
        cached = self._mirror.read(user_id, group_id)
        if cached is not None:
            return cached
        if group is None:
            group = self._groups.get_by_id(group_id)
            if not group:
                return None
        dto = self.build_for_group(group)
        self._mirror.write(user_id, group_id, dto)
        return dto

    def get_active_map(self, user_id: str) -> dict[str, dict]:
        """``{group_id: dto}`` for the user's mirrored active groups (HGETALL)."""
        return self._mirror.read_active(user_id)

    def get_all_active(self) -> list[dict]:
        """Every user's mirrored active groups, each DTO annotated with its
        owning ``user_id``.  Admin dashboard read path — mirror only, no
        Postgres fallback (an empty mirror means nothing is rendering)."""
        return [
            {**dto, "user_id": uid}
            for (uid, _gid), dto in self._mirror.read_all_active().items()
        ]

    # ------------------------------------------------------------------
    # BUILD -- the active DTO (moved from RenderGroupService)
    # ------------------------------------------------------------------

    def build_for_group(self, group: dict[str, Any]) -> dict[str, Any]:
        """Load the group's children and build its active DTO."""
        jobs = self._jobs.get_raw_by_group(group["id"])
        machine_ids = [j["machine_id"] for j in jobs if j.get("machine_id")]
        machines_by_id = self._machines.get_raw_by_ids(machine_ids)
        return self._build_dto(group, jobs, machines_by_id)

    def _build_dto(
        self,
        group: dict[str, Any],
        jobs: list[dict[str, Any]],
        machines_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Full DTO for an active group — derives progress and per-chunk
        ``tasks`` from freshly-loaded children.  Used by the detail/status
        endpoints (single group) and by the list endpoint for the active
        slice (batched children)."""
        resolved_scene = self._scene_resolver.deserialize(
            group.get("resolved_scene_json"),
        )
        resolved_render_settings = resolved_scene.get("render_overrides", {})
        heaviness = resolved_scene.get("heaviness", {})
        scheduling = parse_json_object(group.get("scheduling_json"), {})

        # Pre-fetch the two N+1 sources in bulk: per-job filenames and
        # per-chunk progress.  Each replaces N round-trips (one per job
        # / one per qualifying chunk) with a single query.
        files_by_job = self._output_frames.list_for_group_grouped_by_job(group["id"])
        chunk_progress_by_index = self._chunk_progress.progress_for_group(
            group["id"], jobs,
        )
        retryable_ids = self._compute_retryable_job_ids(
            group, jobs, chunk_progress_by_index,
        )
        tasks = [
            self._serializer.serialize_task(
                job,
                machines_by_id.get(job.get("machine_id")),
                is_retryable=(job["id"] in retryable_ids),
                output_files=files_by_job.get(job["id"], []),
            )
            for job in jobs
        ]

        # Group-level frame counts come from output_frames (dedup'd at INSERT
        # time via PK on (group_id, filename)).  Don't sum task counts --
        # sibling retries that uploaded the same frame would inflate the total.
        total_frames = group.get("total_frames") or 0
        unique_rendered = self._output_frames.count_for_group(group["id"])
        total_rendered = min(total_frames, unique_rendered)

        overall_status = group["status"]

        overall_pct = None
        if total_frames > 0:
            overall_pct = round(min(100.0, total_rendered / total_frames * 100), 1)
        if overall_status == "done":
            overall_pct = 100.0

        latest = self._output_frames.preview_for_group(group["id"])
        latest_output = latest[0] if latest else None
        latest_output_job_id = latest[1] if latest else None

        # Group-level actual-cost rollup -- sum of per-task actuals (credits).
        total_actual_cost_credits = sum(
            (t.get("actual_cost_credits") or 0.0) for t in tasks
        )

        # Group-level ESTIMATED cost: prefer the LLM snapshot captured at
        # submit; fall back to SUM(per-task estimated) for legacy groups.
        snapshot_usd = group.get("pre_render_cost_estimate_usd")
        if snapshot_usd is not None:
            total_estimated_cost_credits = usd_to_credits(
                float(snapshot_usd), self._get_credits_per_usd(),
            )
        else:
            total_estimated_cost_credits = sum(
                (t.get("estimated_cost_credits") or 0.0) for t in tasks
            )

        return {
            "group_id": group["id"],
            "status": overall_status,
            "tier": tiers.normalize(group.get("tier")),
            "input_filename": group["input_filename"],
            "total_frames": total_frames,
            "frame_start": group["frame_start"],
            "frame_end": group["frame_end"],
            "frame_step": group["frame_step"],
            "submitted_at": group.get("submitted_at"),
            "completed_at": group.get("completed_at"),
            "error": group.get("error"),
            "resolved_render_settings": resolved_render_settings,
            "heaviness": heaviness,
            "scheduling": scheduling,
            "overall_rendered_frames": total_rendered,
            "overall_progress_pct": overall_pct,
            "available_output_files_count": min(total_frames, unique_rendered),
            "latest_output_file": latest_output,
            "latest_output_job_id": latest_output_job_id,
            "total_actual_cost_credits": total_actual_cost_credits,
            "total_estimated_cost_credits": total_estimated_cost_credits,
            "tasks_count": len(tasks),
            "tasks": tasks,
        }

    def _compute_retryable_job_ids(
        self,
        group: dict[str, Any],
        jobs: list[dict[str, Any]],
        chunk_progress_by_index: dict[int, ChunkProgress],
    ) -> set[str]:
        """Identify jobs the user can hit "Retry" on: latest failed/cancelled
        attempt for its chunk, auto-retries exhausted, no active sibling, chunk
        not already fully rendered, group not cancelled."""
        if (group.get("status") or "") == "cancelled":
            return set()

        latest_per_chunk: dict[int, str] = {}
        latest_submitted: dict[int, str] = {}
        active_chunks: set[int] = set()
        for j in jobs:
            ci = j.get("chunk_index") or 0
            sub = j.get("submitted_at") or ""
            if ci not in latest_submitted or sub > latest_submitted[ci]:
                latest_submitted[ci] = sub
                latest_per_chunk[ci] = j["id"]
            if (j.get("status") or "") in ("pending", "running"):
                active_chunks.add(ci)

        retryable: set[str] = set()
        for j in jobs:
            if (j.get("status") or "") not in ("failed", "cancelled"):
                continue
            ci = j.get("chunk_index") or 0
            if latest_per_chunk.get(ci) != j["id"]:
                continue
            if (j.get("attempt") or 0) < self._get_max_retries():
                continue
            if ci in active_chunks:
                continue
            progress = chunk_progress_by_index.get(ci)
            if progress is None or progress.is_complete:
                continue
            retryable.add(j["id"])
        return retryable
