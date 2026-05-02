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
    normalize_render_overrides,
    now_iso,
    parse_analysis_heaviness,
    parse_json_list,
    parse_json_object,
    sanitize_filename,
)
from serverV2.infrastructure import storage
from serverV2.infrastructure.auth.firestore_client import write_render_group_record
from serverV2.orchestrator.allocation import tiers
from serverV2.orchestrator.chunk_progress import ChunkProgressService
from serverV2.orchestrator.config import MAX_RETRIES
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.services.assets.serializers import serialize_asset
from serverV2.services.blend_parser.parser import BlendParseError, parse_upload
from serverV2.services.render_groups.frame_planning import resolve_frame_range
from serverV2.services.render_groups.serializers import (
    RenderGroupSerializer,
    build_dispatch_task_entry,
)

log = logging.getLogger(__name__)

_ACTIVE_GROUP_STATUSES = frozenset({"uploading", "pending", "running"})


def _engine_from_snapshot(snapshot) -> str | None:
    """Pull ``heaviness.render_engine`` out of an analyzer snapshot.  Used
    by submission paths to fall back to the .blend's actual engine when
    the user didn't set ``render_overrides.render.engine`` explicitly.
    Returns None if the snapshot doesn't carry the field.
    """
    if not isinstance(snapshot, dict):
        return None
    heaviness_section = snapshot.get("heaviness")
    if not isinstance(heaviness_section, dict):
        return None
    engine = heaviness_section.get("render_engine")
    return engine if isinstance(engine, str) and engine else None


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
        chunk_progress: ChunkProgressService,
    ) -> None:
        self._groups = group_repo
        self._jobs = job_repo
        self._machines = machine_repo
        self._assets = asset_repo
        self._orchestrator = orchestrator
        self._fleet = fleet_registry
        self._outputs = outputs_resolver
        self._output_frames = output_frame_repo
        self._chunk_progress = chunk_progress
        self._serializer = RenderGroupSerializer(output_frame_repo=output_frame_repo)

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

        # Engine resolution at the submission boundary.  User override
        # wins; otherwise fall back to whatever the analyzer detected
        # from the .blend itself.  Resolved here, BEFORE any persistence,
        # and written back into render_overrides so every downstream
        # serialization (group row, job rows, asset cache) carries the
        # engine.  Single source of truth for both initial dispatch and
        # retry — the retry path reads engine from render_overrides_json
        # alone, so this is what makes retry work.
        engine = (
            (render_overrides.get("render") or {}).get("engine")
            or _engine_from_snapshot(analysis_snapshot)
        )
        if not engine:
            raise RenderGroupServiceError(
                400,
                "Render engine could not be determined. Set render.engine in "
                "render_overrides, or ensure the analyzer populated "
                "analysis_snapshot.heaviness.render_engine.",
            )
        render_overrides.setdefault("render", {})["engine"] = engine

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

        # Capture the blend file size as a heaviness signal for the
        # allocator strategy picker (Phase 3).  HEAD-equivalent against R2.
        try:
            r2_input_size_bytes = storage.get_file_size(r2_key)
        except Exception:
            r2_input_size_bytes = None

        # Tier — user-selected allocation tier, normalised to a known value.
        # Persisted on the group row so list/detail responses can show it
        # and so rerenders inherit it by default.
        resolved_tier = tiers.normalize(getattr(payload, "tier", None))

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
            r2_input_size_bytes=r2_input_size_bytes,
            tier=resolved_tier,
            status="pending",
        )
        self._save_asset(group, r2_key, plan, analysis_snapshot, render_overrides, scheduling)

        machine_ids = self._validated_machine_ids(getattr(payload, "machine_ids", None))

        # Build the heaviness dict once from the analyzer's snapshot + the
        # server-side file size; cost-aware strategies read everything they
        # need from this single object.
        heaviness = parse_analysis_heaviness(
            analysis_snapshot if isinstance(analysis_snapshot, dict) else None,
            file_size_bytes=r2_input_size_bytes,
        )

        planned = self._orchestrator.plan(
            frame_start=plan.frame_start,
            frame_end=plan.frame_end,
            frame_step=plan.frame_step,
            total_frames=plan.total_frames,
            machine_ids=machine_ids,
            heaviness=heaviness,
            tier=resolved_tier,
            engine=engine,
        )

        if not planned:
            err = (
                "No eligible render targets. "
                "Pinned community machines are offline, or no serverless capacity available."
                if machine_ids
                else "No eligible render targets. Community machines offline and no serverless capacity available."
            )
            self._groups.full_update(group_id, status="failed", error=err)
            raise RenderGroupServiceError(503, err)

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

        # Engine resolution at the rerender boundary — same single-source
        # rule as confirm_upload.  Resolved BEFORE any persistence and
        # written back into render_overrides so the new group's
        # render_overrides_json (and every job row built from it) carries
        # the engine for both initial dispatch and any later retry.
        original_snapshot = parse_json_object(
            original.get("analysis_snapshot_json") or "{}", {},
        )
        engine = (
            (render_overrides.get("render") or {}).get("engine")
            or _engine_from_snapshot(original_snapshot)
        )
        if not engine:
            raise RenderGroupServiceError(
                400,
                "Render engine could not be determined for rerender. The "
                "original group's analysis_snapshot.heaviness.render_engine "
                "is missing — set render.engine in render_overrides instead.",
            )
        render_overrides.setdefault("render", {})["engine"] = engine

        new_group_id = str(uuid4())
        now = now_iso()

        # Carry the heaviness signal forward — same blend file, same size.
        r2_input_size_bytes = original.get("r2_input_size_bytes")
        if r2_input_size_bytes is None:
            try:
                r2_input_size_bytes = storage.get_file_size(r2_key)
            except Exception:
                r2_input_size_bytes = None

        # Tier — payload override falls back to the original group's tier.
        resolved_tier = tiers.normalize(
            getattr(payload, "tier", None) or original.get("tier")
        )

        from serverV2.infrastructure.db import execute
        execute(
            """
            INSERT INTO render_groups (
                id, input_filename, r2_input_key, r2_input_size_bytes,
                total_frames, frame_start, frame_end, frame_step,
                render_overrides_json, scheduling_json,
                analysis_snapshot_json, analysis_warnings_json,
                status, submitted_at, user_id, source_asset_id, tier
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s)
            """,
            (
                new_group_id, original["input_filename"], r2_key, r2_input_size_bytes,
                total_frames, frame_start, frame_end, frame_step,
                json.dumps(render_overrides), json.dumps(scheduling),
                original.get("analysis_snapshot_json") or "{}",
                original.get("analysis_warnings_json") or "[]",
                now, user["uid"], original.get("source_asset_id"), resolved_tier,
            ),
        )

        machine_ids = self._validated_machine_ids(getattr(payload, "machine_ids", None))

        # Build heaviness once for the rerender (analysis snapshot copied
        # from the original group) + the new r2 file size.
        heaviness = parse_analysis_heaviness(
            original_snapshot,
            file_size_bytes=r2_input_size_bytes,
        )

        planned = self._orchestrator.plan(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            machine_ids=machine_ids,
            heaviness=heaviness,
            tier=resolved_tier,
            engine=engine,
        )

        if not planned:
            err = (
                "No eligible render targets. "
                "Pinned community machines are offline, or no serverless capacity available."
                if machine_ids
                else "No eligible render targets. Community machines offline and no serverless capacity available."
            )
            self._groups.update_status(new_group_id, "failed", error=err)
            raise RenderGroupServiceError(503, err)

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
        jobs = self._jobs.get_raw_by_group(group_id)
        machine_ids = [job["machine_id"] for job in jobs if job.get("machine_id")]
        machines_by_id = self._machines.get_raw_by_ids(machine_ids)
        return self._build_active_status_dto(group, jobs, machines_by_id)

    def list_with_status(self, user_id: str) -> list[dict[str, Any]]:
        """List endpoint hot path.

        Two-tier read: terminal groups serve from snapshot columns on the
        ``render_groups`` row alone (no children loaded).  Active groups
        load their jobs in a single batched query and machines in a single
        batched query — N+1 collapsed to 3 queries total regardless of
        how many groups the user has.
        """
        groups = self._groups.get_by_user(user_id)
        if not groups:
            return []

        active_ids = [g["id"] for g in groups if g.get("status") in _ACTIVE_GROUP_STATUSES]

        jobs_by_group: dict[str, list[dict[str, Any]]] = {}
        machines_by_id: dict[str, dict[str, Any]] = {}
        if active_ids:
            jobs_by_group = self._jobs.get_raw_by_groups(active_ids)
            machine_ids = [
                j["machine_id"]
                for jobs in jobs_by_group.values()
                for j in jobs
                if j.get("machine_id")
            ]
            machines_by_id = self._machines.get_raw_by_ids(machine_ids)

        results: list[dict[str, Any]] = []
        for g in groups:
            if g.get("status") in _ACTIVE_GROUP_STATUSES:
                results.append(self._build_active_status_dto(
                    g, jobs_by_group.get(g["id"], []), machines_by_id,
                ))
            else:
                results.append(self._build_terminal_status_dto(g))
        return results

    def _build_active_status_dto(
        self,
        group: dict[str, Any],
        jobs: list[dict[str, Any]],
        machines_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Full DTO for an active group — derives progress and per-chunk
        ``tasks`` from freshly-loaded children.  Used by the detail/status
        endpoints (single group) and by the list endpoint for the active
        slice (batched children)."""
        resolved_render_settings = normalize_render_overrides(
            parse_json_object(group.get("render_overrides_json"), {})
        )
        scheduling = parse_json_object(group.get("scheduling_json"), {})
        analysis_warnings = parse_json_list(group.get("analysis_warnings_json"), [])
        analysis_snapshot = parse_json_object(group.get("analysis_snapshot_json"), {})

        retryable_ids = self._compute_retryable_job_ids(group, jobs)
        tasks = [
            self._serializer.serialize_task(
                job,
                machines_by_id.get(job.get("machine_id")),
                is_retryable=(job["id"] in retryable_ids),
            )
            for job in jobs
        ]

        # Group-level frame counts come from output_frames (already
        # dedup'd at INSERT time via PK on (group_id, filename)).  Don't
        # sum task counts -- sibling retries that uploaded the same
        # frame would inflate the total.
        total_frames = group.get("total_frames") or 0
        unique_rendered = self._output_frames.count_for_group(group["id"])
        total_rendered = min(total_frames, unique_rendered)

        # Group status is owned by write-side callbacks (success/failure
        # handlers + CallbackRouter on pending→running).  Read endpoints
        # return the stored value directly — no recompute, no write-back.
        overall_status = group["status"]

        overall_pct = None
        if total_frames > 0:
            overall_pct = round(min(100.0, total_rendered / total_frames * 100), 1)
        if overall_status == "done":
            overall_pct = 100.0

        latest = self._output_frames.latest_for_group(group["id"])
        latest_output = latest[0] if latest else None
        latest_output_job_id = latest[1] if latest else None

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
            "scheduling": scheduling,
            "analysis_snapshot": analysis_snapshot,
            "analysis_warnings": analysis_warnings,
            "overall_rendered_frames": total_rendered,
            "overall_progress_pct": overall_pct,
            "available_output_files_count": min(total_frames, unique_rendered),
            "latest_output_file": latest_output,
            "latest_output_job_id": latest_output_job_id,
            "tasks_count": len(tasks),
            "tasks": tasks,
        }

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

    def _compute_retryable_job_ids(
        self,
        group: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> set[str]:
        """Identify jobs the user can hit "Retry" on.  A job qualifies when:
          * status == 'failed'
          * it's the LATEST attempt for its chunk_index (older attempts have
            been superseded — only the latest stuck row gets the button)
          * ``attempt >= MAX_RETRIES`` (auto-retries exhausted, the system
            won't fire on its own)
          * no sibling for the same chunk_index is pending or running
          * the chunk has un-uploaded frames remaining
          * the parent group isn't cancelled

        With this filter, the frontend gets exactly one retryable row per
        stuck chunk — no client-side dedupe needed.
        """
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
            # 'cancelled' qualifies because per-instance cancel
            # (CANCEL_PIPELINE) auto-retries via TryRetryStep, but that
            # retry can hit max-retries-exhausted or no-eligible-target
            # and stop dispatching.  Showing the button on a cancelled
            # chunk lets the user manually re-fire when auto-retry gave
            # up.  Group-level cancel still short-circuits at the top
            # of this method, so a fully aborted render shows no
            # retry buttons.
            if (j.get("status") or "") not in ("failed", "cancelled"):
                continue
            ci = j.get("chunk_index") or 0
            if latest_per_chunk.get(ci) != j["id"]:
                continue
            if (j.get("attempt") or 0) < MAX_RETRIES:
                continue
            if ci in active_chunks:
                continue
            # Skip if the chunk is already fully rendered.  Source-of-truth
            # for "is this chunk done?" is ``ChunkProgressService``, which
            # reads frames uploaded across ALL siblings (not just this job)
            # and compares them against the canonical chunk range from the
            # lowest-attempt sibling.  A chunk where the original sibling
            # rendered everything before being cancelled is correctly
            # excluded even though the latest sibling's per-job count is
            # zero.  Same service the retry pipeline uses, so the UI's
            # ``is_retryable`` and the backend's ``retry_chunk_manually``
            # always agree.
            chunk_siblings = [
                s for s in jobs if (s.get("chunk_index") or 0) == ci
            ]
            progress = self._chunk_progress.progress_for_chunk(
                group["id"], ci, chunk_siblings,
            )
            if progress.is_complete:
                continue
            retryable.add(j["id"])
        return retryable

    def _build_terminal_status_dto(self, group: dict[str, Any]) -> dict[str, Any]:
        """Slim DTO for a terminal group — every per-chunk-derived field
        comes from the snapshot columns on ``render_groups`` (written
        once by the lifecycle when the group entered terminal state).
        ``tasks`` is intentionally empty; the detail page re-loads
        children on demand if the user opens it."""
        resolved_render_settings = normalize_render_overrides(
            parse_json_object(group.get("render_overrides_json"), {})
        )
        scheduling = parse_json_object(group.get("scheduling_json"), {})
        analysis_warnings = parse_json_list(group.get("analysis_warnings_json"), [])
        analysis_snapshot = parse_json_object(group.get("analysis_snapshot_json"), {})

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
            "scheduling": scheduling,
            "analysis_snapshot": analysis_snapshot,
            "analysis_warnings": analysis_warnings,
            "overall_rendered_frames": total_rendered,
            "overall_progress_pct": overall_pct,
            "available_output_files_count": group.get("available_output_files_count") or 0,
            "latest_output_file": group.get("latest_output_file"),
            "latest_output_job_id": group.get("latest_output_job_id"),
            "tasks_count": group.get("tasks_count") or 0,
            "tasks": [],
        }

    # ------------------------------------------------------------------
    # cost preview (Phase 9)
    # ------------------------------------------------------------------

    def estimate_cost(self, group_id: str, user_id: str) -> dict[str, Any]:
        """Per-tier cost + wall-time estimate for a render group.

        Dry-run dispatch — uses the same orchestrator.plan(...) the real
        submit will use, then runs the resulting mix through the cost
        analyzer.  No DB writes.
        """
        from serverV2.services.render_groups.cost_preview import estimate as _estimate

        group = self._groups.get_by_id(group_id)
        if not group:
            raise RenderGroupServiceError(404, "Render group not found")
        if user_id and group.get("user_id") and group["user_id"] != user_id:
            raise RenderGroupServiceError(403, "Forbidden")

        return {
            "group_id": group_id,
            "tiers": _estimate(
                orchestrator=self._orchestrator,
                group_repo=self._groups,
                group_id=group_id,
            ),
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
