"""RenderGroupService — Facade for the full render group lifecycle.

Composes: repositories, storage, upload coordinator, blend parser,
frame planner, orchestrator, fleet registry, and Firestore client.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from serverV2.core.value_objects import (
    MAX_UPLOAD_BYTES,
    SINGLE_PUT_MAX_BYTES,
    extract_analysis_warnings,
    latest_output_filename,
    normalize_render_overrides,
    now_iso,
    parse_json_list,
    parse_json_object,
    sanitize_filename,
)
from serverV2.infrastructure import storage
from serverV2.infrastructure.auth.firestore_client import write_render_group_record
from serverV2.services.assets.serializers import serialize_asset
from serverV2.services.blend_parser.parser import BlendParseError, parse_upload
from serverV2.services.render_groups.frame_planning import resolve_frame_range
from serverV2.services.render_groups.serializers import (
    build_dispatch_task_entry,
    serialize_task,
)

log = logging.getLogger(__name__)


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
    ) -> None:
        self._groups = group_repo
        self._jobs = job_repo
        self._machines = machine_repo
        self._assets = asset_repo
        self._orchestrator = orchestrator
        self._fleet = fleet_registry
        self._outputs = outputs_resolver

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

        render_overrides = normalize_render_overrides(getattr(payload, "render_overrides", None))
        scheduling = getattr(payload, "scheduling", None) or {}
        analysis_snapshot = getattr(payload, "analysis_snapshot", None)
        analysis_snapshot = analysis_snapshot if isinstance(analysis_snapshot, dict) else {}
        analysis_warnings = extract_analysis_warnings(analysis_snapshot)

        timeline = render_overrides.get("timeline", {})

        plan = resolve_frame_range(
            payload_frame_start=getattr(payload, "frame_start", None),
            payload_frame_end=getattr(payload, "frame_end", None),
            payload_frame_step=getattr(payload, "frame_step", None),
            timeline_overrides=timeline,
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
                    render_overrides_json=json.dumps(render_overrides),
                    scheduling_json=json.dumps(scheduling),
                    analysis_snapshot_json=json.dumps(analysis_snapshot),
                    analysis_warnings_json=json.dumps(analysis_warnings),
                )
                self._save_asset(group, r2_key, None, analysis_snapshot, render_overrides, scheduling)
                return {
                    "group_id": group_id,
                    "needs_frame_input": True,
                    "parse_error": str(e),
                    "resolved_render_settings": render_overrides,
                    "scheduling": scheduling,
                    "analysis_warnings": analysis_warnings,
                }
            except Exception:
                raise RenderGroupServiceError(500, "Failed to analyze uploaded file")

        if plan is None or plan.total_frames <= 0:
            raise RenderGroupServiceError(400, "No renderable frames found in .blend file")

        self._groups.full_update(
            group_id,
            total_frames=plan.total_frames,
            frame_start=plan.frame_start,
            frame_end=plan.frame_end,
            frame_step=plan.frame_step,
            render_overrides_json=json.dumps(render_overrides),
            scheduling_json=json.dumps(scheduling),
            analysis_snapshot_json=json.dumps(analysis_snapshot),
            analysis_warnings_json=json.dumps(analysis_warnings),
            status="pending",
        )
        self._save_asset(group, r2_key, plan, analysis_snapshot, render_overrides, scheduling)

        machines = self._resolve_machines(getattr(payload, "machine_ids", None))

        planned = self._orchestrator.plan(
            frame_start=plan.frame_start,
            frame_end=plan.frame_end,
            frame_step=plan.frame_step,
            total_frames=plan.total_frames,
            machines=machines,
        )

        overrides_json = json.dumps(render_overrides)
        dispatch_results = self._orchestrator.execute(
            group_id=group_id,
            input_filename=group["input_filename"],
            tasks=planned,
            render_overrides_json=overrides_json,
        )

        tasks = [
            build_dispatch_task_entry(pt, dr, scheduling)
            for pt, dr in zip(planned, dispatch_results)
        ]

        uid = group.get("user_id")
        if uid:
            try:
                write_render_group_record(uid, group_id, {
                    "group_id": group_id,
                    "filename": group["input_filename"],
                    "status": "pending",
                    "total_frames": plan.total_frames,
                    "frame_start": plan.frame_start,
                    "frame_end": plan.frame_end,
                    "submitted_at": group.get("submitted_at"),
                    "machine_count": len(tasks),
                })
            except Exception:
                pass

        return {
            "group_id": group_id,
            "status": "pending",
            "input_filename": group["input_filename"],
            "total_frames": plan.total_frames,
            "frame_start": plan.frame_start,
            "frame_end": plan.frame_end,
            "frame_step": plan.frame_step,
            "resolved_render_settings": render_overrides,
            "scheduling": scheduling,
            "analysis_warnings": analysis_warnings,
            "tasks": tasks,
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
    # rerender
    # ------------------------------------------------------------------

    def rerender(self, group_id: str, payload: Any, user: dict[str, Any]) -> dict[str, Any]:
        original = self._groups.get_by_id(group_id)
        if not original:
            raise RenderGroupServiceError(404, "Render group not found")
        if not original.get("user_id") or original["user_id"] != user["uid"]:
            raise RenderGroupServiceError(403, "Access denied")

        r2_key = original["r2_input_key"]
        if not storage.file_exists(r2_key):
            raise RenderGroupServiceError(400, "Original input file is no longer in storage")

        frame_start = payload.frame_start
        frame_end = payload.frame_end
        frame_step = max(1, payload.frame_step)
        total_frames = ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0
        if total_frames <= 0:
            raise RenderGroupServiceError(400, "Invalid frame range")

        base_overrides = parse_json_object(original.get("render_overrides_json"), {})
        if getattr(payload, "render_overrides", None):
            base_overrides.update(payload.render_overrides)
        if getattr(payload, "camera", None):
            base_overrides.setdefault("scene", {})["camera"] = payload.camera

        render_overrides = normalize_render_overrides(base_overrides)
        scheduling = parse_json_object(original.get("scheduling_json"), {})

        new_group_id = str(uuid4())
        now = now_iso()

        from serverV2.infrastructure.db import execute
        execute(
            """
            INSERT INTO render_groups (
                id, input_filename, r2_input_key, total_frames,
                frame_start, frame_end, frame_step,
                render_overrides_json, scheduling_json,
                analysis_snapshot_json, analysis_warnings_json,
                status, submitted_at, user_id, source_asset_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
            """,
            (
                new_group_id, original["input_filename"], r2_key,
                total_frames, frame_start, frame_end, frame_step,
                json.dumps(render_overrides), json.dumps(scheduling),
                original.get("analysis_snapshot_json") or "{}",
                original.get("analysis_warnings_json") or "[]",
                now, user["uid"], original.get("source_asset_id"),
            ),
        )

        machines = self._resolve_machines(getattr(payload, "machine_ids", None))

        planned = self._orchestrator.plan(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machines=machines,
        )

        overrides_json = json.dumps(render_overrides)
        dispatch_results = self._orchestrator.execute(
            group_id=new_group_id,
            input_filename=original["input_filename"],
            tasks=planned,
            render_overrides_json=overrides_json,
        )

        tasks = [
            build_dispatch_task_entry(pt, dr, scheduling)
            for pt, dr in zip(planned, dispatch_results)
        ]

        try:
            write_render_group_record(user["uid"], new_group_id, {
                "group_id": new_group_id,
                "filename": original["input_filename"],
                "status": "pending",
                "total_frames": total_frames,
                "frame_start": frame_start,
                "frame_end": frame_end,
                "submitted_at": now,
                "machine_count": len(tasks),
            })
        except Exception:
            pass

        return {
            "group_id": new_group_id,
            "status": "pending",
            "input_filename": original["input_filename"],
            "total_frames": total_frames,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "submitted_at": now,
            "tasks": tasks,
        }

    # ------------------------------------------------------------------
    # get_status
    # ------------------------------------------------------------------

    def get_status(self, group_id: str) -> dict[str, Any]:
        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")

        resolved_render_settings = normalize_render_overrides(
            parse_json_object(group.get("render_overrides_json"), {})
        )
        scheduling = parse_json_object(group.get("scheduling_json"), {})
        analysis_warnings = parse_json_list(group.get("analysis_warnings_json"), [])
        analysis_snapshot = parse_json_object(group.get("analysis_snapshot_json"), {})

        jobs = self._jobs.get_raw_by_group(group_id)

        machine_ids = [job["machine_id"] for job in jobs if job.get("machine_id")]
        machines_by_id = self._machines.get_raw_by_ids(machine_ids)

        tasks = [
            serialize_task(job, machines_by_id.get(job.get("machine_id")))
            for job in jobs
        ]

        statuses = [j["status"] for j in jobs]
        total_frames = group.get("total_frames") or 0
        total_rendered = min(
            total_frames,
            sum(t["rendered_frames"] or 0 for t in tasks),
        )

        from serverV2.callbacks.group_status_aggregator import compute_group_status
        aggregated = compute_group_status(
            current_group_status=group["status"],
            job_statuses=statuses,
            total_frames=total_frames,
            total_rendered=total_rendered,
        )
        overall_status = aggregated.status
        if aggregated.should_persist:
            self._groups.update_status(group_id, overall_status)

        overall_pct = None
        if total_frames > 0:
            overall_pct = round(min(100.0, total_rendered / total_frames * 100), 1)
        if overall_status == "done":
            overall_pct = 100.0

        latest_candidates = [t["latest_output_file"] for t in tasks if t.get("latest_output_file")]

        return {
            "group_id": group_id,
            "status": overall_status,
            "input_filename": group["input_filename"],
            "total_frames": total_frames,
            "frame_start": group["frame_start"],
            "frame_end": group["frame_end"],
            "frame_step": group["frame_step"],
            "submitted_at": group.get("submitted_at"),
            "completed_at": group.get("completed_at"),
            "error": group.get("error"),
            "resolved_render_settings": resolved_render_settings,
            "scheduling": scheduling,
            "analysis_snapshot": analysis_snapshot,
            "analysis_warnings": analysis_warnings,
            "overall_rendered_frames": total_rendered,
            "overall_progress_pct": overall_pct,
            "available_output_files_count": min(
                total_frames, sum(t.get("output_files_count") or 0 for t in tasks),
            ),
            "latest_output_file": latest_output_filename(latest_candidates) if latest_candidates else None,
            "tasks": tasks,
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
            raise RenderGroupServiceError(409, "Only completed, failed, or cancelled groups can be removed")
        self._groups.delete(group_id)
        return {"success": True, "group_id": group_id}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _resolve_machines(self, machine_ids: list[str] | None) -> list:
        if machine_ids:
            machines = []
            for mid in machine_ids:
                m = self._machines.get_by_id(mid)
                if not m:
                    raise RenderGroupServiceError(400, f"Machine {mid[:8]}... not found")
                machines.append(m)
            if not machines:
                raise RenderGroupServiceError(400, "Selected machines are unavailable.")
            return machines
        machines = self._machines.get_available()
        if not machines:
            raise RenderGroupServiceError(400, "No available machines right now. Try again shortly.")
        return machines

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
