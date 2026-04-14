"""
RenderGroupService — thin coordinator the HTTP router talks to.

Holds references to the orchestrator, upload coordinator, and frame planner.
Each public method corresponds to one user-visible action.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from api.routers.assets import upsert_user_input_file
from api.routers.jobs import (
    _sanitize_filename,
    _validate_job_input_filename,
    _validate_upload_size,
    build_job_output_entries,
)
from scheduling.frame_distribution import filter_enabled_machines
from models.render_group import RenderGroup
from scheduling.fleet import compute_group_status
from models.value_objects import (
    MAX_UPLOAD_BYTES,
    SINGLE_PUT_MAX_BYTES,
    compute_progress_pct,
    extract_analysis_warnings,
    is_serverless,
    latest_output_filename,
    normalize_render_overrides,
    normalize_scheduling,
    now_iso,
    output_frame_sort_key,
    parse_json_list,
    parse_json_object,
    parse_output_files,
)
from firebase_auth import write_render_group_record
from infrastructure.db import execute, query_all, query_one
import infrastructure.storage as storage
from scheduling.fleet import fleet
from scheduling.orchestrator import orchestrator
from services.blend_parser import BlendParseError, parse_upload
from services.render_groups.frame_planning import resolve_frame_range

log = logging.getLogger(__name__)


class RenderGroupServiceError(Exception):
    """Raised when a service operation fails.  ``status`` is an HTTP-friendly code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RenderGroupService:

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self, payload: Any, user: dict[str, Any]) -> dict[str, Any]:
        from api.routers.assets import _serialize_input_file_asset

        if payload.machine_ids:
            for mid in payload.machine_ids:
                m = query_one(
                    "SELECT id FROM machines WHERE id = %s AND status = 'available'", (mid,)
                )
                if not m:
                    raise RenderGroupServiceError(400, f"Machine {mid[:8]}... is not available")

        group_id = str(uuid4())
        source_asset = None
        source_asset_id = None
        upload_required = payload.source_asset_id is None
        upload_url = None
        file_size_bytes = None
        multipart_required = False

        if payload.source_asset_id:
            source_asset = query_one(
                "SELECT * FROM user_input_files WHERE id = %s AND user_id = %s",
                (payload.source_asset_id, user["uid"]),
            )
            if not source_asset:
                raise RenderGroupServiceError(404, "Saved input file not found")
            input_filename = _sanitize_filename(source_asset["input_filename"])
            _validate_job_input_filename(input_filename)
            r2_key = source_asset["r2_key"]
            if not r2_key:
                raise RenderGroupServiceError(400, "Saved input file has no storage key")
            if not storage.file_exists(r2_key):
                raise RenderGroupServiceError(400, "Saved input file is missing from storage")
            source_asset_id = source_asset["id"]
            status = "pending"
        else:
            if not payload.filename:
                raise RenderGroupServiceError(
                    400, "filename is required when source_asset_id is not provided"
                )
            input_filename = _sanitize_filename(payload.filename)
            _validate_job_input_filename(input_filename)
            if payload.file_size_bytes is not None:
                file_size_bytes = _validate_upload_size(payload.file_size_bytes)
                multipart_required = file_size_bytes > SINGLE_PUT_MAX_BYTES
            r2_key = f"jobs/{group_id}/input/{input_filename}"
            upload_url = storage.generate_presigned_upload_url(r2_key)
            status = "uploading"

        execute(
            """
            INSERT INTO render_groups (
                id, input_filename, r2_input_key, total_frames,
                frame_start, frame_end, frame_step, status,
                submitted_at, user_id, source_asset_id
            )
            VALUES (%s, %s, %s, 0, 1, 1, 1, %s, %s, %s, %s)
            """,
            (group_id, input_filename, r2_key, status, now_iso(), user["uid"], source_asset_id),
        )

        if source_asset:
            execute(
                "UPDATE user_input_files SET last_used_at = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                (now_iso(), now_iso(), source_asset["id"], user["uid"]),
            )

        source_asset_payload = _serialize_input_file_asset(source_asset) if source_asset else None

        return {
            "group_id": group_id,
            "upload_url": upload_url,
            "r2_key": r2_key,
            "machine_ids": payload.machine_ids or [],
            "upload_required": upload_required,
            "source_asset": source_asset_payload,
            "prefill": source_asset_payload,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "single_put_max_bytes": SINGLE_PUT_MAX_BYTES,
            "multipart_required": multipart_required,
            "suggested_upload_mode": "multipart" if multipart_required else "single_put",
            "file_size_bytes": file_size_bytes,
        }

    # ------------------------------------------------------------------
    # confirm_upload
    # ------------------------------------------------------------------

    def confirm_upload(self, group_id: str, payload: Any, user: dict[str, Any]) -> dict[str, Any]:
        group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")
        if not group.get("user_id") or group["user_id"] != user["uid"]:
            raise RenderGroupServiceError(403, "Access denied")

        execute(
            "UPDATE jobs SET status = 'cancelled', completed_at = %s "
            "WHERE group_id = %s AND status IN ('pending', 'running')",
            (now_iso(), group_id),
        )

        r2_key = group["r2_input_key"]
        if not storage.file_exists(r2_key):
            raise RenderGroupServiceError(400, "File not found in storage")

        render_overrides = normalize_render_overrides(payload.render_overrides)
        scheduling = normalize_scheduling(payload.scheduling)
        analysis_snapshot = payload.analysis_snapshot if isinstance(payload.analysis_snapshot, dict) else {}
        analysis_warnings = extract_analysis_warnings(analysis_snapshot)

        timeline = render_overrides.get("timeline", {})

        plan = resolve_frame_range(
            payload_frame_start=payload.frame_start,
            payload_frame_end=payload.frame_end,
            payload_frame_step=payload.frame_step,
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
                execute(
                    """
                    UPDATE render_groups
                    SET status = 'pending',
                        render_overrides_json = %s, scheduling_json = %s,
                        analysis_snapshot_json = %s, analysis_warnings_json = %s
                    WHERE id = %s
                    """,
                    (
                        json.dumps(render_overrides), json.dumps(scheduling),
                        json.dumps(analysis_snapshot), json.dumps(analysis_warnings),
                        group_id,
                    ),
                )
                if group.get("user_id"):
                    upsert_user_input_file(
                        user_id=group["user_id"],
                        input_filename=group["input_filename"],
                        r2_key=r2_key,
                        analysis_snapshot=analysis_snapshot,
                        render_overrides=render_overrides,
                        scheduling=scheduling,
                        used_at=group.get("submitted_at") or now_iso(),
                    )
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

        execute(
            """
            UPDATE render_groups
            SET total_frames = %s, frame_start = %s, frame_end = %s, frame_step = %s,
                render_overrides_json = %s, scheduling_json = %s,
                analysis_snapshot_json = %s, analysis_warnings_json = %s,
                status = 'pending'
            WHERE id = %s
            """,
            (
                plan.total_frames, plan.frame_start, plan.frame_end, plan.frame_step,
                json.dumps(render_overrides), json.dumps(scheduling),
                json.dumps(analysis_snapshot), json.dumps(analysis_warnings),
                group_id,
            ),
        )
        if group.get("user_id"):
            upsert_user_input_file(
                user_id=group["user_id"],
                input_filename=group["input_filename"],
                r2_key=r2_key,
                frame_start=plan.frame_start,
                frame_end=plan.frame_end,
                frame_step=plan.frame_step,
                analysis_snapshot=analysis_snapshot,
                render_overrides=render_overrides,
                scheduling=scheduling,
                used_at=group.get("submitted_at") or now_iso(),
            )

        machines = self._resolve_machines(payload.machine_ids)

        planned = orchestrator.plan(
            frame_start=plan.frame_start,
            frame_end=plan.frame_end,
            frame_step=plan.frame_step,
            total_frames=plan.total_frames,
            machines=machines,
        )

        overrides_json = json.dumps(render_overrides)
        dispatch_results = orchestrator.execute(
            group_id=group_id,
            input_filename=group["input_filename"],
            tasks=planned,
            render_overrides_json=overrides_json,
            scheduling=scheduling,
        )

        tasks = [
            {
                "job_id": dr.job_id,
                "machine_id": pt.machine_id,
                "machine_gpu": pt.gpu_model,
                "machine_vram": pt.gpu_vram_gb,
                "frame_start": pt.frame_start,
                "frame_end": pt.frame_end,
                "frame_step": pt.frame_step,
                "chunk_index": pt.chunk_index,
                "total_frames": pt.total_frames,
                "rendered_frames": 0,
                "progress_pct": None,
                "status": "pending",
                "power_score": pt.power_score,
                "error": None,
                "attempt": 0,
                "max_retries": scheduling.get("max_retries_per_chunk", 0),
                "priority": scheduling.get("priority", 0),
            }
            for pt, dr in zip(planned, dispatch_results)
        ]

        try:
            write_render_group_record(user["uid"], group_id, {
                "group_id": group_id,
                "filename": group["input_filename"],
                "status": "pending",
                "total_frames": plan.total_frames,
                "frame_start": plan.frame_start,
                "frame_end": plan.frame_end,
                "submitted_at": group["submitted_at"],
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

    def delete(self, group_id: str, user: dict[str, Any]) -> dict[str, Any]:
        group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")
        if not group.get("user_id") or group["user_id"] != user["uid"]:
            raise RenderGroupServiceError(403, "Access denied")
        if group["status"] not in ("done", "failed", "cancelled"):
            raise RenderGroupServiceError(
                409, "Only completed, failed, or cancelled render groups can be removed"
            )
        execute("DELETE FROM jobs WHERE group_id = %s", (group_id,))
        execute("DELETE FROM render_groups WHERE id = %s", (group_id,))
        return {"success": True, "group_id": group_id}

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    def cancel(self, group_id: str) -> dict[str, Any]:
        from scheduling.strategies import get_strategy as _get_strategy

        group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")

        cancelled_at = now_iso()
        jobs = query_all(
            "SELECT * FROM jobs WHERE group_id = %s AND status IN ('pending', 'running')",
            (group_id,),
        )

        execute(
            "UPDATE render_groups SET status = 'cancelled', completed_at = %s WHERE id = %s",
            (cancelled_at, group_id),
        )
        for job in jobs:
            execute(
                "UPDATE jobs SET status = 'cancelled', completed_at = %s, error = 'Cancelled by user' WHERE id = %s",
                (now_iso(), job["id"]),
            )
            mt = self._machine_type_of(job["machine_id"])
            if not is_serverless(mt):
                execute(
                    "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
                    (now_iso(), job["machine_id"]),
                )

        import threading
        from services import vast as _vast
        from services import modal as _modal

        def _cancel_providers() -> None:
            for job in jobs:
                mt = self._machine_type_of(job["machine_id"])
                log.info(f"[CANCEL DEBUG] job={job['id']} machine_type={mt}")

                # Remove from in-memory registry immediately so the UI reflects
                # the cancellation without waiting for the next poll tick.
                if mt == "vast_serverless":
                    _vast.remove_instance(job["id"])
                elif mt == "modal_serverless":
                    _modal.remove_instance(job["id"])

                strategy = _get_strategy(mt)
                pid = strategy.provider_job_id_from_job(job)
                log.info(f"[CANCEL DEBUG] job={job['id']} provider_job_id={pid!r}")
                if not pid:
                    log.warning(f"[CANCEL DEBUG] job={job['id']} has no provider_job_id — skipping cancel")
                    continue
                if strategy.is_enabled():
                    try:
                        log.info(f"[CANCEL DEBUG] calling strategy.cancel(pid={pid!r})")
                        strategy.cancel(pid, job["machine_id"])
                        log.info(f"[CANCEL DEBUG] strategy.cancel returned for pid={pid!r}")
                    except Exception as exc:
                        log.warning(f"Failed to cancel provider job {pid}: {exc}")
                else:
                    log.warning(f"[CANCEL DEBUG] strategy for {mt} is not enabled — skipping cancel")

        threading.Thread(target=_cancel_providers, daemon=True, name=f"cancel-{group_id[:8]}").start()
        return {"success": True, "cancelled_jobs": len(jobs)}

    # ------------------------------------------------------------------
    # rerender
    # ------------------------------------------------------------------

    def rerender(self, group_id: str, payload: Any, user: dict[str, Any]) -> dict[str, Any]:
        original = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
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
        if payload.render_overrides:
            base_overrides.update(payload.render_overrides)
        if payload.camera:
            base_overrides.setdefault("scene", {})["camera"] = payload.camera

        render_overrides = normalize_render_overrides(base_overrides)
        scheduling = normalize_scheduling(parse_json_object(original.get("scheduling_json"), {}))
        analysis_snapshot = parse_json_object(original.get("analysis_snapshot_json"), {})
        analysis_warnings = parse_json_list(original.get("analysis_warnings_json"), [])

        new_group_id = str(uuid4())
        now = now_iso()

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
                json.dumps(analysis_snapshot), json.dumps(analysis_warnings),
                now, user["uid"], original.get("source_asset_id"),
            ),
        )

        machines = fleet.get_available_machines()
        if not machines:
            raise RenderGroupServiceError(400, "No available machines right now. Try again shortly.")

        planned = orchestrator.plan(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machines=machines,
        )

        overrides_json = json.dumps(render_overrides)
        dispatch_results = orchestrator.execute(
            group_id=new_group_id,
            input_filename=original["input_filename"],
            tasks=planned,
            render_overrides_json=overrides_json,
            scheduling=scheduling,
        )

        tasks = [
            {
                "job_id": dr.job_id,
                "machine_id": pt.machine_id,
                "machine_gpu": pt.gpu_model,
                "machine_vram": pt.gpu_vram_gb,
                "frame_start": pt.frame_start,
                "frame_end": pt.frame_end,
                "frame_step": pt.frame_step,
                "total_frames": pt.total_frames,
                "rendered_frames": 0,
                "status": "pending",
            }
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
    # get_status (READ-ONLY — no DB mutations)
    # ------------------------------------------------------------------

    def get_status(self, group_id: str) -> dict[str, Any]:
        group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")

        resolved_render_settings = normalize_render_overrides(
            parse_json_object(group.get("render_overrides_json"), {})
        )
        scheduling = normalize_scheduling(parse_json_object(group.get("scheduling_json"), {}))
        analysis_warnings = parse_json_list(group.get("analysis_warnings_json"), [])
        analysis_snapshot = parse_json_object(group.get("analysis_snapshot_json"), {})

        jobs = query_all(
            "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC", (group_id,)
        )

        tasks = [
            self._serialize_task(
                job, query_one("SELECT * FROM machines WHERE id = %s", (job["machine_id"],))
            )
            for job in jobs
        ]

        statuses = [j["status"] for j in jobs]
        total_frames = group["total_frames"] or 0
        total_rendered = min(
            total_frames,
            sum(t["rendered_frames"] or 0 for t in tasks if t["status"] != "failed"),
        )

        aggregated = compute_group_status(
            current_group_status=group["status"],
            job_statuses=statuses,
            total_frames=total_frames,
            total_rendered=total_rendered,
        )
        overall_status = aggregated.status
        if aggregated.should_persist:
            if overall_status == "running":
                execute("UPDATE render_groups SET status = 'running' WHERE id = %s", (group_id,))
            else:
                execute(
                    "UPDATE render_groups SET status = %s, completed_at = %s WHERE id = %s",
                    (overall_status, now_iso(), group_id),
                )

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
            "submitted_at": group["submitted_at"],
            "completed_at": group.get("completed_at"),
            "error": group.get("error"),
            "resolved_render_settings": resolved_render_settings,
            "scheduling": scheduling,
            "analysis_snapshot": analysis_snapshot,
            "analysis_warnings": analysis_warnings,
            "overall_rendered_frames": total_rendered,
            "overall_progress_pct": overall_pct,
            "available_output_files_count": min(
                total_frames,
                sum(t.get("output_files_count") or 0 for t in tasks),
            ),
            "latest_output_file": latest_output_filename(latest_candidates) if latest_candidates else None,
            "tasks": tasks,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _resolve_machines(self, machine_ids: list[str] | None) -> list[dict[str, Any]]:
        if machine_ids:
            machines: list[dict[str, Any]] = []
            for mid in machine_ids:
                m = query_one("SELECT * FROM machines WHERE id = %s", (mid,))
                if not m:
                    raise RenderGroupServiceError(400, f"Machine {mid[:8]}... not found")
                machines.append(m)
            machines = filter_enabled_machines(machines)
            if not machines:
                raise RenderGroupServiceError(
                    400, "Selected machines are currently disabled or unavailable."
                )
            return machines
        machines = fleet.get_available_machines()
        if not machines:
            raise RenderGroupServiceError(400, "No available machines right now. Try again shortly.")
        return machines

    @staticmethod
    def _machine_type_of(machine_id: str) -> str:
        row = query_one("SELECT machine_type FROM machines WHERE id = %s", (machine_id,))
        return row["machine_type"] if row else "windows"

    @staticmethod
    def _serialize_task(job: dict[str, Any], machine: dict[str, Any] | None = None) -> dict[str, Any]:
        total_frames = job.get("total_frames")
        rendered_frames = max(0, job.get("rendered_frames") or 0)
        if total_frames and total_frames > 0:
            rendered_frames = min(rendered_frames, total_frames)
        output_files = parse_output_files(job.get("output_files"))

        actual_gpu = job.get("actual_gpu_name")
        actual_vram = job.get("actual_gpu_vram_gb")

        if actual_gpu:
            vram_suffix = f" {int(actual_vram)}GB" if actual_vram else ""
            display_gpu = f"Vast {actual_gpu}{vram_suffix}"
            display_vram = actual_vram or (machine.get("gpu_vram_gb", 0) if machine else 0)
        else:
            display_gpu = machine["gpu_model"] if machine else "Unknown"
            display_vram = machine.get("gpu_vram_gb", 0) if machine else 0

        return {
            "job_id": job["id"],
            "runpod_job_id": job.get("runpod_job_id"),
            "machine_id": job["machine_id"],
            "machine_type": machine.get("machine_type", "windows") if machine else "windows",
            "machine_gpu": display_gpu,
            "machine_vram": display_vram,
            "frame_start": job.get("frame_start"),
            "frame_end": job.get("frame_end"),
            "frame_step": job.get("frame_step") or 1,
            "chunk_index": job.get("chunk_index"),
            "total_frames": total_frames,
            "rendered_frames": rendered_frames,
            "progress_pct": compute_progress_pct(
                job.get("status", ""), rendered_frames, total_frames
            ),
            "status": job.get("status", "pending"),
            "error": job.get("error"),
            "attempt": job.get("attempt") or 0,
            "max_retries": job.get("max_retries") or 0,
            "priority": job.get("priority") or 0,
            "output_files_count": len(output_files),
            "latest_output_file": latest_output_filename(output_files),
        }


render_group_service = RenderGroupService()
