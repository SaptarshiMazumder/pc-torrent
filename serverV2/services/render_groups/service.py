"""RenderGroupService — Facade for the full render group lifecycle.

Composes: repositories, storage, upload coordinator, blend parser,
frame planner, orchestrator, fleet registry, and Firestore client.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable
from uuid import uuid4

from serverV2.config import usd_to_credits
from serverV2.core.value_objects import (
    MAX_UPLOAD_BYTES,
    RENDER_PRIORITY_DEFAULT,
    SINGLE_PUT_MAX_BYTES,
    clamp_render_priority,
    now_iso,
    parse_json_object,
    sanitize_filename,
)
from serverV2.infrastructure import storage
from serverV2.allocation.allocation_strategies.allocation_helpers import allocation_tiers as tiers
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.services.assets.serializers import serialize_asset
from serverV2.services.blend_parser.parser import BlendParseError, parse_upload
from serverV2.services.pre_render import SceneResolver, resolve_frame_range
from serverV2.services.pre_render.scene_resolver import SceneResolutionError

log = logging.getLogger(__name__)

_ACTIVE_GROUP_STATUSES = frozenset({"uploading", "pending", "running"})


class RenderGroupServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RenderGroupService:

    def __init__(
        self,
        *,
        group_repo,
        job_repo,
        machine_repo,
        asset_repo,
        orchestrator,
        fleet_registry,
        outputs_resolver,
        output_frame_repo: OutputFrameRepository,
        scene_resolver: SceneResolver,
        get_credits_per_usd: Callable[[], float],
        telemetry,
    ) -> None:
        self._groups = group_repo
        self._jobs = job_repo
        self._machines = machine_repo
        self._assets = asset_repo
        self._orchestrator = orchestrator
        self._fleet = fleet_registry
        self._outputs = outputs_resolver
        self._output_frames = output_frame_repo
        self._scene_resolver = scene_resolver
        # Live Firestore knob: read per call so admin edits to
        # billing.credits_per_usd take effect immediately on the terminal
        # DTO's actual-cost projection.
        self._get_credits_per_usd = get_credits_per_usd
        # Owns the active-group DTO + its Redis mirror.  get_status / list
        # delegate the ACTIVE path here; RenderGroupService keeps terminal.
        self._telemetry = telemetry

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self, payload: Any, user: dict[str, Any]) -> dict[str, Any]:
        if getattr(payload, "machine_ids", None):
            for mid in payload.machine_ids:
                m = self._machines.get_by_id(mid)
                if not m or m.status != "available":
                    raise RenderGroupServiceError(400, f"Machine {mid[:8]}... is not available")

        group_id = str(uuid4())
        source_asset = None
        source_asset_id = None
        upload_required = getattr(payload, "source_asset_id", None) is None
        upload_url = None
        file_size_bytes = None
        multipart_required = False

        if getattr(payload, "source_asset_id", None):
            source_asset = self._assets.get_by_id(payload.source_asset_id, user["uid"])
            if not source_asset:
                raise RenderGroupServiceError(404, "Saved input file not found")
            input_filename = sanitize_filename(source_asset["input_filename"])
            r2_key = source_asset["r2_key"]
            if not r2_key or not storage.file_exists(r2_key):
                raise RenderGroupServiceError(400, "Saved input file is missing from storage")
            source_asset_id = source_asset["id"]
            status = "pending"
        else:
            if not getattr(payload, "filename", None):
                raise RenderGroupServiceError(400, "filename is required when source_asset_id is not provided")
            input_filename = sanitize_filename(payload.filename)
            if getattr(payload, "file_size_bytes", None) is not None:
                from serverV2.services.upload.validators import validate_upload_size
                file_size_bytes = validate_upload_size(payload.file_size_bytes)
                multipart_required = file_size_bytes > SINGLE_PUT_MAX_BYTES
            r2_key = f"jobs/{group_id}/input/{input_filename}"
            upload_url = storage.generate_presigned_upload_url(r2_key)
            status = "uploading"

        self._groups.create(
            group_id=group_id,
            input_filename=input_filename,
            r2_input_key=r2_key,
            status=status,
            user_id=user["uid"],
            source_asset_id=source_asset_id,
        )

        return {
            "group_id": group_id,
            "upload_url": upload_url,
            "r2_key": r2_key,
            "machine_ids": getattr(payload, "machine_ids", None) or [],
            "upload_required": upload_required,
            "source_asset": serialize_asset(source_asset) if source_asset else None,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "single_put_max_bytes": SINGLE_PUT_MAX_BYTES,
            "multipart_required": multipart_required,
            "suggested_upload_mode": "multipart" if multipart_required else "single_put",
            "file_size_bytes": file_size_bytes,
        }

    # ------------------------------------------------------------------
    # input download (workers pull the blend from the group's r2_input_key,
    # which may live under jobs/{gid}/input/ OR under an asset's r2_key if
    # the render reused a previously-uploaded file)
    # ------------------------------------------------------------------

    def get_input_download_url(self, group_id: str) -> str:
        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")
        key = group.get("r2_input_key")
        if not key or not storage.file_exists(key):
            raise RenderGroupServiceError(404, "Input file not found")
        return storage.generate_presigned_url(key)

    def resolve_input_key(self, group_id: str) -> str:
        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")
        key = group.get("r2_input_key")
        if not key:
            raise RenderGroupServiceError(404, "Render group has no input key")
        return key

    # ------------------------------------------------------------------
    # confirm_upload
    # ------------------------------------------------------------------

    def confirm_upload(self, group_id: str, payload: Any) -> dict[str, Any]:
        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")

        self._jobs.cancel_active_by_group(group_id)

        r2_key = group["r2_input_key"]
        if not storage.file_exists(r2_key):
            raise RenderGroupServiceError(400, "File not found in storage")

        analysis_snapshot = getattr(payload, "analysis_snapshot", None)
        analysis_snapshot = analysis_snapshot if isinstance(analysis_snapshot, dict) else {}
        raw_overrides = getattr(payload, "render_overrides", None)
        scheduling = getattr(payload, "scheduling", None) or {}

        # File-size signal for heaviness — server-side fact, the desktop
        # analyzer can't see it.  HEAD-equivalent against R2.
        try:
            r2_input_size_bytes = storage.get_file_size(r2_key)
        except Exception:
            r2_input_size_bytes = None

        # Submission boundary: collapse (snapshot, overrides) to ONE
        # canonical resolved scene.  Engine resolution + heaviness merge
        # happen exactly here — every post-submit reader sees the row.
        try:
            resolved_scene = self._scene_resolver.resolve(
                analysis_snapshot=analysis_snapshot,
                render_overrides=raw_overrides,
                file_size_bytes=r2_input_size_bytes,
            )
        except SceneResolutionError as exc:
            raise RenderGroupServiceError(400, str(exc))

        normalized_overrides = resolved_scene["render_overrides"]
        heaviness = resolved_scene["heaviness"]
        engine = heaviness["render_engine"]

        plan = resolve_frame_range(
            payload_frame_start=getattr(payload, "frame_start", None),
            payload_frame_end=getattr(payload, "frame_end", None),
            payload_frame_step=getattr(payload, "frame_step", None),
            timeline_overrides=normalized_overrides.get("timeline"),
        )

        if plan is None:
            try:
                file_data = storage.download_file(r2_key)
                frame_info = parse_upload(file_data, group["input_filename"])
                plan = resolve_frame_range(
                    payload_frame_start=None,
                    payload_frame_end=None,
                    payload_frame_step=None,
                    timeline_overrides=None,
                    parsed_frame_info=frame_info,
                )
            except BlendParseError as e:
                self._groups.full_update(
                    group_id,
                    status="pending",
                    resolved_scene_json=self._scene_resolver.serialize(resolved_scene),
                    scheduling_json=json.dumps(scheduling),
                )
                self._save_asset(group, r2_key, None, analysis_snapshot, normalized_overrides, scheduling)
                return {
                    "group_id": group_id,
                    "needs_frame_input": True,
                    "parse_error": str(e),
                    "resolved_render_settings": normalized_overrides,
                    "scheduling": scheduling,
                }
            except Exception:
                raise RenderGroupServiceError(500, "Failed to analyze uploaded file")

        if plan is None or plan.total_frames <= 0:
            raise RenderGroupServiceError(400, "No renderable frames found in .blend file")

        # Tier — user-selected allocation tier, normalised to a known value.
        # Persisted on the group row so list/detail responses can show it.
        resolved_tier = tiers.normalize(getattr(payload, "tier", None))

        # Priority — user-selected queue ordering key.  Pydantic has
        # already clamped via Field(ge=LOW, le=HIGH); the clamp here is
        # the belt-and-braces fallback for callers (tests, future
        # internal submits) that bypass the API validator.
        priority = clamp_render_priority(
            getattr(payload, "priority", RENDER_PRIORITY_DEFAULT),
        )

        self._groups.full_update(
            group_id,
            total_frames=plan.total_frames,
            frame_start=plan.frame_start,
            frame_end=plan.frame_end,
            frame_step=plan.frame_step,
            resolved_scene_json=self._scene_resolver.serialize(resolved_scene),
            scheduling_json=json.dumps(scheduling),
            r2_input_size_bytes=r2_input_size_bytes,
            tier=resolved_tier,
            status="pending",
        )
        self._save_asset(group, r2_key, plan, analysis_snapshot, normalized_overrides, scheduling)

        machine_ids = self._validated_machine_ids(getattr(payload, "machine_ids", None))

        # Worker contract: render_overrides_json passed into dispatch is
        # the user-overrides slice of the resolved scene (timeline,
        # output, render.engine, camera_*, etc.).  Heaviness stays
        # server-side; workers don't need it.
        overrides_json = json.dumps(normalized_overrides)

        # LLM-derived cost snapshot.  Server re-runs the same dry-run
        # cost the UI just showed (formula cache populated by the
        # pre-render call moments earlier -- no LLM API call fires
        # here on the hot path) and stamps the total on the group row.
        # The post-submit "ESTIMATED COST" surface reads from this
        # column instead of summing the heuristic-derived per-chunk
        # stamps on jobs rows, so the user sees the same number
        # pre-submit AND during render.
        #
        # No try/except here on purpose -- LLM flakiness is already
        # contained inside cost_for_dry_run, so any failure here is
        # a real bug (DB, planner, shape) and should fail submit
        # loudly rather than silently leave the snapshot column NULL.
        snapshot = self._orchestrator.cost_estimate_for_dry_run(
            frame_start=plan.frame_start,
            frame_end=plan.frame_end,
            frame_step=plan.frame_step,
            total_frames=plan.total_frames,
            engine=engine,
            heaviness=heaviness,
            priority=priority,
        )
        self._groups.set_pre_render_cost_estimate_usd(
            group_id, snapshot.total_cost_usd,
        )

        # Park the group on pending_allocation_queue.  The dispatch
        # daemon plans + dispatches against its tick-local mutable
        # snapshot on the next tick -- the only thread allowed to
        # touch fleet availability state, so no cross-thread races.
        # The UI receives this synchronous ack, navigates to the jobs
        # list, and polls ``GET /render-groups/{id}`` to watch chunks
        # land as the daemon dispatches.
        self._orchestrator.submit_initial(
            group_id=group_id,
            frame_start=plan.frame_start,
            frame_end=plan.frame_end,
            frame_step=plan.frame_step,
            total_frames=plan.total_frames,
            machine_ids=machine_ids,
            heaviness=heaviness,
            engine=engine,
            tier=resolved_tier,
            priority=priority,
            input_filename=group["input_filename"],
            render_overrides_json=overrides_json,
        )

        return {
            "group_id": group_id,
            "status": "pending",
            "input_filename": group["input_filename"],
            "total_frames": plan.total_frames,
            "frame_start": plan.frame_start,
            "frame_end": plan.frame_end,
            "frame_step": plan.frame_step,
            "resolved_render_settings": normalized_overrides,
            "scheduling": scheduling,
            "tasks": [],
        }

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    def cancel(self, group_id: str) -> dict[str, Any]:
        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")
        result = self._orchestrator.cancel_group(group_id)
        return {"success": True, **result}

    # ------------------------------------------------------------------
    # get_status
    # ------------------------------------------------------------------

    def get_status(self, group_id: str) -> dict[str, Any]:
        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")
        if group.get("status") in _ACTIVE_GROUP_STATUSES:
            # Active: served from the Redis mirror (Postgres build on miss).
            return self._telemetry.get_live(group.get("user_id"), group_id, group)
        # Terminal detail: full DTO with tasks (client caches it); not mirrored.
        return self._telemetry.build_for_group(group)

    def list_with_status_page(
        self, user_id: str, *, limit: int, offset: int,
        status_group: str | None = None,
    ) -> dict[str, Any]:
        """List endpoint hot path -- paginated.

        Two-tier read: terminal groups serve from snapshot columns on the
        ``render_groups`` row alone (no children loaded).  Active groups
        in the page slice load their jobs in a single batched query and
        machines in a single batched query — N+1 collapsed to 3 queries
        regardless of page size.

        ``status_group`` (``"active"`` | ``"terminal"`` | ``None``) lets
        callers paginate the two halves of the user's history
        independently, so the desktop's "Ongoing" and "Past" sections
        each get their own offset cursor.

        Returns ``{"groups": [...], "has_more": bool}``.
        """
        groups, has_more = self._groups.get_by_user_page(
            user_id, limit=limit, offset=offset, status_group=status_group,
        )
        if not groups:
            return {"groups": [], "has_more": False}

        has_active = any(
            g.get("status") in _ACTIVE_GROUP_STATUSES for g in groups
        )
        # One HGETALL for the user's mirrored active groups; misses (not yet
        # mirrored) fall back to a per-group build that also populates the mirror.
        active_map = self._telemetry.get_active_map(user_id) if has_active else {}

        results: list[dict[str, Any]] = []
        for g in groups:
            if g.get("status") in _ACTIVE_GROUP_STATUSES:
                dto = active_map.get(g["id"])
                if dto is None:
                    dto = self._telemetry.get_live(user_id, g["id"], g)
                results.append(dto)
            else:
                results.append(self._build_terminal_status_dto(g))
        return {"groups": results, "has_more": has_more}

    def download_all_as_zip(self, group_id: str):
        """Stream every uploaded frame for the group into a single ZIP.
        Filenames come from the ``output_frames`` table — no scanning of
        per-job JSON, no double-counting sibling-retry duplicates."""
        import io
        import zipfile
        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Group not found")
        filenames = self._output_frames.list_for_group(group_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                key = f"jobs/{group_id}/output/{fname}"
                try:
                    data = storage.download_file(key)
                    zf.writestr(fname, data)
                except Exception as exc:
                    log.warning("Skipping %s in ZIP for group %s: %s", fname, group_id, exc)
        buf.seek(0)
        return buf

    def _build_terminal_status_dto(self, group: dict[str, Any]) -> dict[str, Any]:
        """Slim DTO for a terminal group — every per-chunk-derived field
        comes from the snapshot columns on ``render_groups`` (written
        once by the lifecycle when the group entered terminal state).
        ``tasks`` is intentionally empty; the detail page re-loads
        children on demand if the user opens it."""
        resolved_scene = self._scene_resolver.deserialize(
            group.get("resolved_scene_json"),
        )
        resolved_render_settings = resolved_scene.get("render_overrides", {})
        heaviness = resolved_scene.get("heaviness", {})
        scheduling = parse_json_object(group.get("scheduling_json"), {})

        total_frames = group.get("total_frames") or 0
        total_rendered = group.get("overall_rendered_frames") or 0

        overall_pct = None
        if total_frames > 0:
            overall_pct = round(min(100.0, total_rendered / total_frames * 100), 1)
        if group.get("status") == "done":
            overall_pct = 100.0

        return {
            "group_id": group["id"],
            "status": group["status"],
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
            "available_output_files_count": group.get("available_output_files_count") or 0,
            "latest_output_file": group.get("latest_output_file"),
            "latest_output_job_id": group.get("latest_output_job_id"),
            # Actual cost frozen on the row at terminal transition (USD),
            # projected to credits here -- mirrors the active DTO's actual-cost
            # field so the list reads the same key for every group.  Legacy
            # terminal groups (NULL actual) project to 0; the Cost column then
            # renders "—" (no estimate fallback -- show the real cost or
            # nothing).
            "total_actual_cost_credits": usd_to_credits(
                float(group.get("total_actual_cost_usd") or 0.0),
                self._get_credits_per_usd(),
            ),
            "tasks_count": group.get("tasks_count") or 0,
            "tasks": [],
        }

    # ------------------------------------------------------------------
    # outputs
    # ------------------------------------------------------------------

    def get_outputs(self, group_id: str) -> dict[str, Any]:
        if not self._groups.get_by_id(group_id):
            raise RenderGroupServiceError(404, "Render group not found")

        jobs = self._jobs.get_raw_by_group(group_id)
        entries = self._outputs.entries(jobs, scope_id=group_id)
        return {"group_id": group_id, "files": entries, "count": len(entries)}

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def delete(self, group_id: str, user: dict[str, Any]) -> dict[str, Any]:
        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")
        if not group.get("user_id") or group["user_id"] != user["uid"]:
            raise RenderGroupServiceError(403, "Access denied")
        if group["status"] not in ("done", "failed", "cancelled"):
            self._orchestrator.cancel_group(group_id)
        self._groups.delete(group_id)
        return {"success": True, "group_id": group_id}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _validated_machine_ids(self, machine_ids: list[str] | None) -> list[str] | None:
        """Validate user-supplied community machine ids if any.

        Returns None when the user did not pin specific machines (the
        orchestrator will then consider the full pool of community boxes
        plus enabled serverless capabilities).
        """
        if not machine_ids:
            return None
        for mid in machine_ids:
            m = self._machines.get_by_id(mid)
            if not m:
                raise RenderGroupServiceError(400, f"Machine {mid[:8]}... not found")
        return list(machine_ids)

    def _save_asset(self, group, r2_key, plan, analysis_snapshot, render_overrides, scheduling):
        uid = group.get("user_id")
        if not uid:
            return
        try:
            self._assets.upsert(
                user_id=uid,
                input_filename=group["input_filename"],
                r2_key=r2_key,
                frame_start=plan.frame_start if plan else None,
                frame_end=plan.frame_end if plan else None,
                frame_step=plan.frame_step if plan else None,
                analysis_snapshot_json=json.dumps(analysis_snapshot),
                render_overrides_json=json.dumps(render_overrides),
                scheduling_json=json.dumps(scheduling),
                used_at=group.get("submitted_at") or now_iso(),
            )
        except Exception as exc:
            log.warning("Failed to save user input file asset: %s", exc)
