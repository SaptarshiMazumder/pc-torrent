"""
Render group routes: creation, upload, job dispatch, status, outputs, cancellation.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.routers.assets import upsert_user_input_file
from api.routers.jobs import (
    _sanitize_filename,
    _validate_job_input_filename,
    _validate_upload_size,
    _choose_multipart_part_size,
    _compute_total_parts,
    _normalize_completed_parts,
    _normalize_part_numbers,
    _machine_type_of,
    build_job_output_entries,
)
from api.schemas.job import (
    MultipartAbortPayload,
    MultipartCompletePayload,
    MultipartInitPayload,
    MultipartPartUrlsPayload,
)
from api.schemas.render_group import (
    ConfirmRenderGroupPayload,
    CreateRenderGroupPayload,
    ReRenderPayload,
)
from scheduling.frame_distributor import (
    choose_retry_machine,
    distribute_frames,
    distribute_frames_by_chunk_size,
    expand_serverless_assignments,
    filter_enabled_machines,
    get_available_machines,
)
from scheduling.dispatch_coordinator import coordinator
from scheduling.strategies import get_strategy
from domain.value_objects import (
    FAILOVER_STALE_SECONDS,
    MAX_UPLOAD_BYTES,
    SINGLE_PUT_MAX_BYTES,
    compute_progress_pct,
    extract_analysis_warnings,
    is_serverless,
    latest_output_filename,
    now_iso,
    normalize_render_overrides,
    normalize_scheduling,
    output_frame_sort_key,
    parse_json_list,
    parse_json_object,
    parse_output_files,
)
from firebase_auth import get_current_user, write_render_group_record
from infrastructure.db import execute, query_all, query_one, request_conn
import infrastructure.storage as storage
from services.blend_parser import BlendParseError, parse_upload

log = logging.getLogger(__name__)

router = APIRouter(tags=["render_groups"])


# ---------------------------------------------------------------------------
# Ownership guard
# ---------------------------------------------------------------------------

def _ensure_owner(group: dict[str, Any], current_user: dict) -> None:
    if not group.get("user_id") or group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _serialize_render_group_task(
    job: dict[str, Any], machine: dict[str, Any] | None = None
) -> dict[str, Any]:
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
        "chunk_size_frames": job.get("chunk_size_frames"),
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


def _build_render_group_output_entries(group_id: str) -> list[dict[str, Any]]:
    jobs = query_all(
        """
        SELECT id, status, output_files, frame_start, submitted_at
        FROM jobs WHERE group_id = %s
        ORDER BY submitted_at ASC, frame_start ASC
        """,
        (group_id,),
    )
    dedup: dict[str, dict[str, Any]] = {}
    for job in jobs:
        for item in build_job_output_entries(job):
            dedup[item["filename"]] = item
    entries = list(dedup.values())
    entries.sort(key=lambda item: output_frame_sort_key(item.get("filename", "")))
    return entries


# ---------------------------------------------------------------------------
# Failover helper (stale desktop detection)
# ---------------------------------------------------------------------------

SERVERLESS_PENDING_TIMEOUT_SEC = 300  # 5 min without a poller → orphaned

def _check_failover(group_id: str, tasks_raw: list[dict[str, Any]]) -> list[str]:
    """
    Detect stale machines (desktop or serverless) and reassign their tasks.
    Returns list of newly created job IDs.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=FAILOVER_STALE_SECONDS)
    ).isoformat()
    serverless_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=SERVERLESS_PENDING_TIMEOUT_SEC)
    ).isoformat()
    new_job_ids: list[str] = []

    machine_ids = list({t["machine_id"] for t in tasks_raw})
    machines_map = {
        mid: m
        for mid in machine_ids
        for m in [query_one("SELECT * FROM machines WHERE id = %s", (mid,))]
        if m
    }

    # Detect orphaned serverless jobs: "pending" for too long means dispatch
    # failed without marking the job, or the poller thread never started.
    for task in tasks_raw:
        if task["status"] not in ("pending", "running"):
            continue
        machine = machines_map.get(task["machine_id"])
        if not machine or not is_serverless(machine.get("machine_type", "")):
            continue

        # Serverless "running" job with all frames rendered → mark done
        total = task.get("total_frames") or 0
        rendered = task.get("rendered_frames") or 0
        if task["status"] == "running" and total > 0 and rendered >= total:
            log.info(
                f"Serverless job {task['id']} has {rendered}/{total} frames "
                f"rendered but still 'running' — marking done"
            )
            execute(
                "UPDATE jobs SET status = 'done', completed_at = %s, "
                "rendered_frames = %s WHERE id = %s",
                (now_iso(), total, task["id"]),
            )
            continue

        # Serverless "pending" for too long → dispatch failed silently
        if task["status"] == "pending":
            submitted = task.get("submitted_at") or ""
            if not submitted or submitted >= serverless_cutoff:
                continue
            log.warning(
                f"Serverless job {task['id']} stuck in 'pending' since {submitted} — "
                f"marking failed (dispatch likely failed silently)"
            )
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                ("Dispatch timed out — no instance was created", now_iso(), task["id"]),
            )

    for task in tasks_raw:
        if task["status"] != "running":
            continue
        machine = machines_map.get(task["machine_id"])
        if not machine:
            continue
        if is_serverless(machine.get("machine_type", "")):
            continue
        if (machine.get("last_seen_at") or "") >= cutoff:
            continue

        execute(
            "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
            ("Machine went offline", now_iso(), task["id"]),
        )

        rendered = max(0, task.get("rendered_frames") or 0)
        step = task.get("frame_step") or 1
        new_start = task["frame_start"] + rendered * step
        new_end = task["frame_end"]

        if new_start > new_end:
            continue

        best_machine_id = choose_retry_machine(group_id, task["machine_id"])
        if not best_machine_id or best_machine_id == task["machine_id"]:
            continue
        best_machine = query_one("SELECT * FROM machines WHERE id = %s", (best_machine_id,))
        if not best_machine:
            continue

        new_job_id = str(uuid4())
        new_total = ((new_end - new_start) // step) + 1
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, group_id, input_filename, status,
                total_frames, rendered_frames, output_files,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, chunk_size_frames, submitted_at
            )
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_job_id, best_machine["id"], group_id,
                task["input_filename"], new_total,
                new_start, new_end, step,
                task.get("render_overrides_json") or "{}",
                task.get("attempt") or 0, task.get("max_retries") or 0,
                task.get("priority") or 0,
                task.get("chunk_index"), task.get("chunk_size_frames"),
                now_iso(),
            ),
        )
        new_job_ids.append(new_job_id)

        best_machine_type = best_machine.get("machine_type", "windows")
        if is_serverless(best_machine_type):
            group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
            if group:
                from services import runpod_dispatch
                blend_url = (
                    f"{runpod_dispatch.PUBLIC_BACKEND_URL}"
                    f"/render-groups/{group_id}/input/{group['input_filename']}"
                )
                overrides_b64 = base64.b64encode(
                    (task.get("render_overrides_json") or "{}").encode()
                ).decode()
                try:
                    coordinator.dispatch(
                        job_id=new_job_id,
                        machine_id=best_machine["id"],
                        machine_type=best_machine_type,
                        blend_url=blend_url,
                        frame_start=new_start,
                        frame_end=new_end,
                        frame_step=step,
                        render_overrides_b64=overrides_b64,
                        group_id=group_id,
                    )
                except Exception as exc:
                    log.error(f"Failover dispatch failed for {new_job_id}: {exc}")

    # Retry failed serverless jobs that still have unrendered frames.
    # Build a set of frame ranges already covered by non-failed jobs so we
    # don't create duplicate retries, and count failures per range to cap retries.
    SERVERLESS_MAX_RETRIES = 5

    covered_ranges: set[tuple[int, int]] = set()
    failed_range_counts: dict[tuple[int, int], int] = {}
    for t in tasks_raw:
        rng = (t["frame_start"], t["frame_end"])
        if t["status"] == "failed":
            failed_range_counts[rng] = failed_range_counts.get(rng, 0) + 1
        else:
            covered_ranges.add(rng)

    group_row = None  # lazy-loaded if needed

    for task in tasks_raw:
        if task["status"] != "failed":
            continue
        machine = machines_map.get(task["machine_id"])
        if not machine or not is_serverless(machine.get("machine_type", "")):
            continue

        rendered = max(0, task.get("rendered_frames") or 0)
        step = task.get("frame_step") or 1
        new_start = task["frame_start"] + rendered * step
        new_end = task["frame_end"]
        if new_start > new_end:
            continue

        if (new_start, new_end) in covered_ranges:
            continue

        rng = (task["frame_start"], task["frame_end"])
        if failed_range_counts.get(rng, 0) >= SERVERLESS_MAX_RETRIES:
            continue

        best_machine_id = choose_retry_machine(group_id, task["machine_id"])
        if not best_machine_id:
            continue
        best_machine = query_one("SELECT * FROM machines WHERE id = %s", (best_machine_id,))
        if not best_machine:
            continue

        new_job_id = str(uuid4())
        new_total = ((new_end - new_start) // step) + 1
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, group_id, input_filename, status,
                total_frames, rendered_frames, output_files,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, chunk_size_frames, submitted_at
            )
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_job_id, best_machine["id"], group_id,
                task["input_filename"], new_total,
                new_start, new_end, step,
                task.get("render_overrides_json") or "{}",
                (task.get("attempt") or 0) + 1, task.get("max_retries") or 0,
                task.get("priority") or 0,
                task.get("chunk_index"), task.get("chunk_size_frames"),
                now_iso(),
            ),
        )
        new_job_ids.append(new_job_id)
        covered_ranges.add((new_start, new_end))

        best_machine_type = best_machine.get("machine_type", "windows")
        if is_serverless(best_machine_type):
            if group_row is None:
                group_row = query_one(
                    "SELECT * FROM render_groups WHERE id = %s", (group_id,)
                )
            if group_row:
                from services import runpod_dispatch
                blend_url = (
                    f"{runpod_dispatch.PUBLIC_BACKEND_URL}"
                    f"/render-groups/{group_id}/input/{group_row['input_filename']}"
                )
                overrides_b64 = base64.b64encode(
                    (task.get("render_overrides_json") or "{}").encode()
                ).decode()
                try:
                    coordinator.dispatch(
                        job_id=new_job_id,
                        machine_id=best_machine["id"],
                        machine_type=best_machine_type,
                        blend_url=blend_url,
                        frame_start=new_start,
                        frame_end=new_end,
                        frame_step=step,
                        render_overrides_b64=overrides_b64,
                        group_id=group_id,
                    )
                except Exception as exc:
                    log.error(f"Serverless retry dispatch failed for {new_job_id}: {exc}")
                    execute(
                        "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                        (f"Retry dispatch failed: {exc}", now_iso(), new_job_id),
                    )
        log.info(
            f"Created serverless retry job {new_job_id} for failed {task['id']} "
            f"(frames {new_start}-{new_end} on {best_machine.get('gpu_model', '?')})"
        )

    return new_job_ids


# ---------------------------------------------------------------------------
# Core render group status reader
# ---------------------------------------------------------------------------

def _get_render_group_inner(group_id: str) -> dict[str, Any]:
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")

    resolved_render_settings = normalize_render_overrides(
        parse_json_object(group.get("render_overrides_json"), {})
    )
    scheduling = normalize_scheduling(parse_json_object(group.get("scheduling_json"), {}))
    analysis_warnings = parse_json_list(group.get("analysis_warnings_json"), [])
    analysis_snapshot = parse_json_object(group.get("analysis_snapshot_json"), {})

    jobs = query_all(
        "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC", (group_id,)
    )

    # Force-terminate orphaned non-terminal jobs in cancelled/failed groups.
    # This catches race conditions where a job was missed by the cancel handler
    # or where a poller exited without marking the job terminal.
    if group["status"] in ("cancelled", "failed"):
        orphaned = [j for j in jobs if j["status"] in ("pending", "running")]
        if orphaned:
            for j in orphaned:
                rendered = j.get("rendered_frames") or 0
                total = j.get("total_frames") or 0
                if total > 0 and rendered >= total:
                    execute(
                        "UPDATE jobs SET status = 'done', completed_at = %s, "
                        "rendered_frames = %s WHERE id = %s",
                        (now_iso(), total, j["id"]),
                    )
                    log.info(f"Orphaned job {j['id']} had all frames rendered — marked done")
                else:
                    execute(
                        "UPDATE jobs SET status = %s, completed_at = %s, "
                        "error = %s WHERE id = %s",
                        (group["status"], now_iso(),
                         f"Group was {group['status']}", j["id"]),
                    )
                    log.info(f"Orphaned job {j['id']} force-{group['status']}")
            jobs = query_all(
                "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC",
                (group_id,),
            )

    if group["status"] in ("pending", "running"):
        _check_failover(group_id, jobs)
        jobs = query_all(
            "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC", (group_id,)
        )

    tasks = [
        _serialize_render_group_task(
            job, query_one("SELECT * FROM machines WHERE id = %s", (job["machine_id"],))
        )
        for job in jobs
    ]

    statuses = [j["status"] for j in jobs]
    total_frames = group["total_frames"] or 0
    # Only count rendered_frames from non-failed jobs.  Failed jobs' frames are
    # accounted for in their failover successor's frame_start, so summing them
    # too causes double-counting when multiple failover hops each render partials.
    total_rendered = min(
        total_frames,
        sum(t["rendered_frames"] or 0 for t in tasks if t["status"] != "failed"),
    )
    no_active = not any(s in ("pending", "running") for s in statuses)

    # Cancelled/failed group: if all frames were rendered, treat as "done";
    # otherwise the group's terminal status is authoritative.
    if group["status"] in ("cancelled", "failed"):
        if (
            no_active
            and any(s == "done" for s in statuses)
            and total_frames > 0
            and total_rendered >= total_frames
        ):
            overall_status = "done"
            if group["status"] != "done":
                execute(
                    "UPDATE render_groups SET status = 'done', completed_at = %s WHERE id = %s",
                    (now_iso(), group_id),
                )
        else:
            overall_status = group["status"]
    elif all(s == "done" for s in statuses) or (
        no_active
        and any(s == "done" for s in statuses)
        and total_frames > 0
        and total_rendered >= total_frames
    ):
        overall_status = "done"
        if group["status"] != "done":
            execute(
                "UPDATE render_groups SET status = 'done', completed_at = %s WHERE id = %s",
                (now_iso(), group_id),
            )
    elif any(s == "running" for s in statuses):
        overall_status = "running"
        if group["status"] == "pending":
            execute(
                "UPDATE render_groups SET status = 'running' WHERE id = %s", (group_id,)
            )
    elif no_active and all(s == "failed" for s in statuses):
        overall_status = "failed"
        if group["status"] != "failed":
            execute(
                "UPDATE render_groups SET status = 'failed', completed_at = %s WHERE id = %s",
                (now_iso(), group_id),
            )
    else:
        overall_status = group["status"]

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


# ---------------------------------------------------------------------------
# Routes — creation & upload
# ---------------------------------------------------------------------------

@router.post("/render-groups/create")
def create_render_group(
    payload: CreateRenderGroupPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    from api.routers.assets import _serialize_input_file_asset

    if payload.machine_ids:
        for mid in payload.machine_ids:
            m = query_one(
                "SELECT id FROM machines WHERE id = %s AND status = 'available'", (mid,)
            )
            if not m:
                raise HTTPException(
                    status_code=400, detail=f"Machine {mid[:8]}... is not available"
                )

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
            (payload.source_asset_id, current_user["uid"]),
        )
        if not source_asset:
            raise HTTPException(status_code=404, detail="Saved input file not found")
        input_filename = _sanitize_filename(source_asset["input_filename"])
        _validate_job_input_filename(input_filename)
        r2_key = source_asset["r2_key"]
        if not r2_key:
            raise HTTPException(status_code=400, detail="Saved input file has no storage key")
        if not storage.file_exists(r2_key):
            raise HTTPException(
                status_code=400, detail="Saved input file is missing from storage"
            )
        source_asset_id = source_asset["id"]
        status = "pending"
    else:
        if not payload.filename:
            raise HTTPException(
                status_code=400,
                detail="filename is required when source_asset_id is not provided",
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
        (group_id, input_filename, r2_key, status, now_iso(), current_user["uid"], source_asset_id),
    )

    if source_asset:
        execute(
            "UPDATE user_input_files SET last_used_at = %s, updated_at = %s WHERE id = %s AND user_id = %s",
            (now_iso(), now_iso(), source_asset["id"], current_user["uid"]),
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


@router.post("/render-groups/{group_id}/multipart-upload/init")
def init_render_group_multipart_upload(
    group_id: str,
    payload: MultipartInitPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    group = query_one(
        "SELECT id, r2_input_key, status, user_id FROM render_groups WHERE id = %s", (group_id,)
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    _ensure_owner(group, current_user)
    if group["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Render group is not in uploading state")

    file_size_bytes = _validate_upload_size(payload.file_size_bytes)
    part_size_bytes = _choose_multipart_part_size(file_size_bytes, payload.part_size_bytes)
    total_parts = _compute_total_parts(file_size_bytes, part_size_bytes)
    r2_key = group["r2_input_key"]

    try:
        upload_id = storage.create_multipart_upload(
            r2_key, content_type=(payload.content_type or "application/octet-stream")
        )
    except Exception as exc:
        log.error("Failed to create multipart upload for render group %s: %s", group_id, exc)
        raise HTTPException(status_code=500, detail="Failed to initialize multipart upload")

    return {
        "upload_id": upload_id,
        "part_size_bytes": part_size_bytes,
        "total_parts": total_parts,
        "file_size_bytes": file_size_bytes,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "single_put_max_bytes": SINGLE_PUT_MAX_BYTES,
        "r2_key": r2_key,
    }


@router.post("/render-groups/{group_id}/multipart-upload/part-urls")
def render_group_multipart_part_urls(
    group_id: str,
    payload: MultipartPartUrlsPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    group = query_one(
        "SELECT id, r2_input_key, status, user_id FROM render_groups WHERE id = %s", (group_id,)
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    _ensure_owner(group, current_user)
    if group["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Render group is not in uploading state")

    r2_key = group["r2_input_key"]
    numbers = _normalize_part_numbers(payload.part_numbers)
    urls = {
        str(n): storage.generate_presigned_upload_part_url(
            r2_key, payload.upload_id, n
        )
        for n in numbers
    }
    return {"upload_id": payload.upload_id, "urls": urls}


@router.post("/render-groups/{group_id}/multipart-upload/complete")
def complete_render_group_multipart_upload(
    group_id: str,
    payload: MultipartCompletePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    group = query_one(
        "SELECT id, r2_input_key, status, user_id FROM render_groups WHERE id = %s", (group_id,)
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    _ensure_owner(group, current_user)
    if group["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Render group is not in uploading state")

    parts = _normalize_completed_parts(payload.parts)
    try:
        result = storage.complete_multipart_upload(group["r2_input_key"], payload.upload_id, parts)
    except Exception as exc:
        log.error("Failed to complete multipart upload for render group %s: %s", group_id, exc)
        raise HTTPException(
            status_code=400, detail=f"Failed to complete multipart upload: {exc}"
        )
    return {
        "success": True,
        "upload_id": payload.upload_id,
        "etag": result.get("ETag"),
        "location": result.get("Location"),
        "key": result.get("Key"),
    }


@router.post("/render-groups/{group_id}/multipart-upload/abort")
def abort_render_group_multipart_upload(
    group_id: str,
    payload: MultipartAbortPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    group = query_one(
        "SELECT id, r2_input_key, user_id FROM render_groups WHERE id = %s", (group_id,)
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    _ensure_owner(group, current_user)
    try:
        storage.abort_multipart_upload(group["r2_input_key"], payload.upload_id)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Failed to abort multipart upload: {exc}"
        )
    return {"success": True, "upload_id": payload.upload_id}


# ---------------------------------------------------------------------------
# Routes — confirm upload & job creation
# ---------------------------------------------------------------------------

@router.post("/render-groups/{group_id}/confirm-upload")
def confirm_render_group_upload(
    group_id: str,
    payload: ConfirmRenderGroupPayload,
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Confirm upload, parse .blend, distribute frames, and create jobs."""
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    _ensure_owner(group, current_user)

    # Cancel any leftover jobs from a previous attempt so they don't overlap
    execute(
        "UPDATE jobs SET status = 'cancelled', completed_at = %s "
        "WHERE group_id = %s AND status IN ('pending', 'running')",
        (now_iso(), group_id),
    )

    r2_key = group["r2_input_key"]
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=400, detail="File not found in storage")

    render_overrides = normalize_render_overrides(payload.render_overrides)
    scheduling = normalize_scheduling(payload.scheduling)
    analysis_snapshot = payload.analysis_snapshot if isinstance(payload.analysis_snapshot, dict) else {}
    analysis_warnings = extract_analysis_warnings(analysis_snapshot)

    timeline = render_overrides.get("timeline", {})

    # Resolve frame range: explicit payload > render_overrides.timeline > parse file
    if payload.frame_start is not None and payload.frame_end is not None:
        frame_start = int(payload.frame_start)
        frame_end = int(payload.frame_end)
        frame_step = int(payload.frame_step or 1)
    elif timeline.get("frame_start") is not None and timeline.get("frame_end") is not None:
        frame_start = int(timeline["frame_start"])
        frame_end = int(timeline["frame_end"])
        frame_step = int(timeline.get("frame_step") or 1)
    else:
        try:
            file_data = storage.download_file(r2_key)
            frame_info = parse_upload(file_data, group["input_filename"])
            frame_start = frame_info["frame_start"]
            frame_end = frame_info["frame_end"]
            frame_step = frame_info["frame_step"]
        except BlendParseError as e:
            execute(
                """
                UPDATE render_groups
                SET status = 'pending',
                    render_overrides_json = %s,
                    scheduling_json = %s,
                    analysis_snapshot_json = %s,
                    analysis_warnings_json = %s
                WHERE id = %s
                """,
                (
                    json.dumps(render_overrides),
                    json.dumps(scheduling),
                    json.dumps(analysis_snapshot),
                    json.dumps(analysis_warnings),
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
            raise HTTPException(status_code=500, detail="Failed to analyze uploaded file")

    frame_step = max(1, frame_step)
    total_frames = (
        ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0
    )
    if total_frames <= 0:
        raise HTTPException(status_code=400, detail="No renderable frames found in .blend file")

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
            total_frames, frame_start, frame_end, frame_step,
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
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            analysis_snapshot=analysis_snapshot,
            render_overrides=render_overrides,
            scheduling=scheduling,
            used_at=group.get("submitted_at") or now_iso(),
        )

    if payload.machine_ids:
        machines: list[dict[str, Any]] = []
        for mid in payload.machine_ids:
            m = query_one("SELECT * FROM machines WHERE id = %s", (mid,))
            if not m:
                raise HTTPException(status_code=400, detail=f"Machine {mid[:8]}... not found")
            machines.append(m)
        machines = filter_enabled_machines(machines)
        if not machines:
            raise HTTPException(
                status_code=400,
                detail="Selected machines are currently disabled or unavailable.",
            )
    else:
        machines = get_available_machines()
        if not machines:
            raise HTTPException(
                status_code=400, detail="No available machines right now. Try again shortly."
            )

    # Frame distribution
    chunk_size_frames = scheduling.get("chunk_size_frames")
    if chunk_size_frames:
        assignments = distribute_frames_by_chunk_size(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            machines=machines,
            chunk_size_frames=chunk_size_frames,
        )
    else:
        assignments = distribute_frames(total_frames, frame_start, frame_end, frame_step, machines)
        for i, a in enumerate(assignments):
            a["chunk_index"] = i
            a["chunk_size_frames"] = None

    assignments = expand_serverless_assignments(assignments)
    for i, a in enumerate(assignments):
        a["chunk_index"] = i

    try:
        counts: dict[str, int] = {}
        for a in assignments:
            mt = a.get("machine_type", "unknown")
            counts[mt] = counts.get(mt, 0) + 1
        log.info(f"Render group {group_id}: assignments by machine_type = {counts}")
    except Exception:
        pass

    # Create job rows
    max_retries = scheduling.get("max_retries_per_chunk", 0)
    priority = scheduling.get("priority", 0)
    overrides_json = json.dumps(render_overrides)
    tasks = []

    for a in assignments:
        job_id = str(uuid4())
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, group_id, input_filename, status,
                total_frames, rendered_frames, output_files,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, chunk_size_frames, submitted_at
            )
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                job_id, a["machine_id"], group_id, group["input_filename"],
                a["total_frames"], a["frame_start"], a["frame_end"], a["frame_step"],
                overrides_json, 0, max_retries, priority,
                a.get("chunk_index"), a.get("chunk_size_frames"),
                now_iso(),
            ),
        )
        if not is_serverless(a.get("machine_type", "")):
            execute(
                "UPDATE machines SET status = 'processing' WHERE id = %s", (a["machine_id"],)
            )
        tasks.append({
            "job_id": job_id,
            "machine_id": a["machine_id"],
            "machine_gpu": a["gpu_model"],
            "machine_vram": a["gpu_vram_gb"],
            "frame_start": a["frame_start"],
            "frame_end": a["frame_end"],
            "frame_step": a["frame_step"],
            "chunk_index": a.get("chunk_index"),
            "chunk_size_frames": a.get("chunk_size_frames"),
            "total_frames": a["total_frames"],
            "rendered_frames": 0,
            "progress_pct": None,
            "status": "pending",
            "power_score": a["power_score"],
            "error": None,
            "attempt": 0,
            "max_retries": max_retries,
            "priority": priority,
        })

    # Dispatch serverless tasks
    from services import runpod_dispatch
    from services import modal as modal_dispatch
    from services import vast as vast_dispatch

    overrides_b64 = base64.b64encode(overrides_json.encode()).decode()
    blend_url_base = (
        f"{runpod_dispatch.PUBLIC_BACKEND_URL}"
        f"/render-groups/{group_id}/input/{group['input_filename']}"
    )

    runpod_strategy = get_strategy("runpod_serverless")
    if runpod_strategy.is_enabled():
        for i, task in enumerate(tasks):
            if _machine_type_of(task["machine_id"]) != "runpod_serverless":
                continue
            if i > 0:
                time.sleep(0.15)
            try:
                coordinator.dispatch(
                    job_id=task["job_id"],
                    machine_id=task["machine_id"],
                    machine_type="runpod_serverless",
                    blend_url=blend_url_base,
                    frame_start=task["frame_start"],
                    frame_end=task["frame_end"],
                    frame_step=task["frame_step"],
                    render_overrides_b64=overrides_b64,
                    group_id=group_id,
                )
            except Exception as exc:
                log.error(f"Failed to dispatch job {task['job_id']} to RunPod: {exc}")

    modal_strategy = get_strategy("modal_serverless")
    if modal_strategy.is_enabled():
        modal_blend_url = (
            f"{modal_dispatch.PUBLIC_BACKEND_URL}"
            f"/render-groups/{group_id}/input/{group['input_filename']}"
        )
        modal_tasks = [
            t for t in tasks
            if _machine_type_of(t["machine_id"]) == "modal_serverless"
        ]

        def _dispatch_modal(task: dict):
            try:
                coordinator.dispatch(
                    job_id=task["job_id"],
                    machine_id=task["machine_id"],
                    machine_type="modal_serverless",
                    blend_url=modal_blend_url,
                    frame_start=task["frame_start"],
                    frame_end=task["frame_end"],
                    frame_step=task["frame_step"],
                    render_overrides_b64=overrides_b64,
                    group_id=group_id,
                )
            except Exception as exc:
                log.error(f"Failed to dispatch job {task['job_id']} to Modal: {exc}")
                execute(
                    "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                    (f"Dispatch failed: {exc}", now_iso(), task["job_id"]),
                )

        for i, task in enumerate(modal_tasks):
            if i > 0:
                time.sleep(0.05)
            threading.Thread(
                target=_dispatch_modal,
                args=(task,),
                daemon=True,
                name=f"modal-dispatch-{task['job_id'][:8]}",
            ).start()

    vast_strategy = get_strategy("vast_serverless")
    if vast_strategy.is_enabled():
        vast_blend_url = (
            f"{vast_dispatch.PUBLIC_BACKEND_URL}"
            f"/render-groups/{group_id}/input/{group['input_filename']}"
        )
        vast_tasks = [
            t for t in tasks
            if _machine_type_of(t["machine_id"]) == "vast_serverless"
        ]

        def _dispatch_vast(task: dict):
            try:
                coordinator.dispatch(
                    job_id=task["job_id"],
                    machine_id=task["machine_id"],
                    machine_type="vast_serverless",
                    blend_url=vast_blend_url,
                    frame_start=task["frame_start"],
                    frame_end=task["frame_end"],
                    frame_step=task["frame_step"],
                    render_overrides_b64=overrides_b64,
                    group_id=group_id,
                )
            except Exception as exc:
                log.error(f"Failed to dispatch job {task['job_id']} to Vast.ai: {exc}")
                execute(
                    "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                    (f"Dispatch failed: {exc}", now_iso(), task["job_id"]),
                )

        def _dispatch_vast_sequential():
            for task in vast_tasks:
                _dispatch_vast(task)
                time.sleep(2)

        threading.Thread(
            target=_dispatch_vast_sequential,
            daemon=True,
            name=f"vast-dispatch-group-{group_id[:8]}",
        ).start()

    try:
        write_render_group_record(current_user["uid"], group_id, {
            "group_id": group_id,
            "filename": group["input_filename"],
            "status": "pending",
            "total_frames": total_frames,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "submitted_at": group["submitted_at"],
            "machine_count": len(tasks),
        })
    except Exception:
        pass

    return {
        "group_id": group_id,
        "status": "pending",
        "input_filename": group["input_filename"],
        "total_frames": total_frames,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frame_step": frame_step,
        "resolved_render_settings": render_overrides,
        "scheduling": scheduling,
        "analysis_warnings": analysis_warnings,
        "tasks": tasks,
    }


# ---------------------------------------------------------------------------
# Routes — status & outputs
# ---------------------------------------------------------------------------

@router.get("/render-groups")
def list_render_groups(
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    groups = query_all(
        "SELECT id FROM render_groups WHERE user_id = %s ORDER BY submitted_at DESC",
        (current_user["uid"],),
    )
    with request_conn():
        return [_get_render_group_inner(g["id"]) for g in groups]


@router.get("/render-groups/{group_id}")
def get_render_group(
    group_id: str, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    group = query_one("SELECT user_id FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group.get("user_id") and group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    with request_conn():
        return _get_render_group_inner(group_id)


@router.delete("/render-groups/{group_id}")
def delete_render_group(
    group_id: str, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group.get("user_id") and group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if group["status"] not in ("done", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail="Only completed, failed, or cancelled render groups can be removed",
        )
    execute("DELETE FROM jobs WHERE group_id = %s", (group_id,))
    execute("DELETE FROM render_groups WHERE id = %s", (group_id,))
    return {"success": True, "group_id": group_id}


@router.get("/render-groups/{group_id}/input/{filename}")
def download_render_group_input_file(group_id: str, filename: str):
    group = query_one(
        "SELECT input_filename, r2_input_key FROM render_groups WHERE id = %s", (group_id,)
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    safe_name = _sanitize_filename(filename)
    canonical_name = _sanitize_filename(group.get("input_filename") or safe_name)
    if safe_name != canonical_name:
        raise HTTPException(status_code=404, detail="File not found")
    r2_key = group.get("r2_input_key") or f"jobs/{group_id}/input/{canonical_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")
    return RedirectResponse(
        url=storage.generate_presigned_url(r2_key, download_name=canonical_name)
    )


@router.get("/render-groups/{group_id}/outputs")
def list_render_group_outputs(
    group_id: str, current_user: dict = Depends(get_current_user)
):
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group.get("user_id") and group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    files = _build_render_group_output_entries(group_id)
    return {"group_id": group_id, "status": group.get("status"), "files": files, "count": len(files)}


@router.get("/render-groups/{group_id}/download")
def download_render_group_output(
    group_id: str, current_user: dict = Depends(get_current_user)
):
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group.get("user_id") and group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")

    all_jobs = query_all("SELECT status FROM jobs WHERE group_id = %s", (group_id,))
    if any(j["status"] in ("pending", "running") for j in all_jobs):
        raise HTTPException(status_code=400, detail="Render group has tasks still in progress")

    files = _build_render_group_output_entries(group_id)
    if not files:
        raise HTTPException(status_code=404, detail="No output files found")
    return {"files": files, "group_id": group_id}


# ---------------------------------------------------------------------------
# Routes — cancellation
# ---------------------------------------------------------------------------

@router.post("/render-groups/cancel-all")
def cancel_all_render_groups() -> dict[str, Any]:
    groups = query_all(
        "SELECT id FROM render_groups WHERE status IN ('pending', 'running', 'uploading')"
    )
    total_cancelled = 0
    for g in groups:
        result = cancel_render_group(g["id"])
        total_cancelled += result.get("cancelled_jobs", 0)
    return {"success": True, "cancelled_groups": len(groups), "cancelled_jobs": total_cancelled}


@router.post("/render-groups/{group_id}/cancel")
def cancel_render_group(group_id: str) -> dict[str, Any]:
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")

    cancelled_at = now_iso()

    jobs = query_all(
        "SELECT * FROM jobs WHERE group_id = %s AND status IN ('pending', 'running')",
        (group_id,),
    )

    # Mark group and all active jobs cancelled in DB immediately so:
    # 1. The response returns fast (no blocking on provider API calls)
    # 2. The polling threads see 'cancelled' on their next cycle and stop
    execute(
        "UPDATE render_groups SET status = 'cancelled', completed_at = %s WHERE id = %s",
        (cancelled_at, group_id),
    )
    for job in jobs:
        execute(
            "UPDATE jobs SET status = 'cancelled', completed_at = %s, error = 'Cancelled by user' WHERE id = %s",
            (now_iso(), job["id"]),
        )
        job_machine_type = _machine_type_of(job["machine_id"])
        if not is_serverless(job_machine_type):
            execute(
                "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
                (now_iso(), job["machine_id"]),
            )

    # Destroy provider instances in a background thread — fire-and-forget.
    # The DB update above is what actually stops the work; this is cleanup.
    def _cancel_providers() -> None:
        for job in jobs:
            job_machine_type = _machine_type_of(job["machine_id"])
            strategy = get_strategy(job_machine_type)
            provider_job_id = strategy.provider_job_id_from_job(job)
            if not provider_job_id:
                continue
            if strategy.is_enabled():
                try:
                    strategy.cancel(provider_job_id, job["machine_id"])
                except Exception as exc:
                    log.warning(f"Failed to cancel provider job {provider_job_id}: {exc}")

    threading.Thread(target=_cancel_providers, daemon=True, name=f"cancel-{group_id[:8]}").start()

    return {"success": True, "cancelled_jobs": len(jobs)}


# ---------------------------------------------------------------------------
# Routes — re-render
# ---------------------------------------------------------------------------

@router.post("/render-groups/{group_id}/rerender")
def rerender_group(
    group_id: str,
    payload: ReRenderPayload,
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a new render group from an existing one with a custom frame range."""
    original = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not original:
        raise HTTPException(status_code=404, detail="Render group not found")
    _ensure_owner(original, current_user)

    r2_key = original["r2_input_key"]
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=400, detail="Original input file is no longer in storage")

    frame_start = payload.frame_start
    frame_end = payload.frame_end
    frame_step = max(1, payload.frame_step)
    total_frames = ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0
    if total_frames <= 0:
        raise HTTPException(status_code=400, detail="Invalid frame range")

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
            now, current_user["uid"], original.get("source_asset_id"),
        ),
    )

    machines = get_available_machines()
    if not machines:
        raise HTTPException(
            status_code=400, detail="No available machines right now. Try again shortly."
        )

    chunk_size_frames = scheduling.get("chunk_size_frames")
    if chunk_size_frames:
        assignments = distribute_frames_by_chunk_size(
            frame_start=frame_start, frame_end=frame_end, frame_step=frame_step,
            machines=machines, chunk_size_frames=chunk_size_frames,
        )
    else:
        assignments = distribute_frames(total_frames, frame_start, frame_end, frame_step, machines)
        for i, a in enumerate(assignments):
            a["chunk_index"] = i
            a["chunk_size_frames"] = None

    assignments = expand_serverless_assignments(assignments)
    for i, a in enumerate(assignments):
        a["chunk_index"] = i

    max_retries = scheduling.get("max_retries_per_chunk", 0)
    priority = scheduling.get("priority", 0)
    overrides_json = json.dumps(render_overrides)
    tasks = []

    for a in assignments:
        job_id = str(uuid4())
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, group_id, input_filename, status,
                total_frames, rendered_frames, output_files,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, chunk_size_frames, submitted_at, user_id
            )
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]',
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                job_id, a["machine_id"], new_group_id, original["input_filename"],
                a["total_frames"], a["frame_start"], a["frame_end"], a["frame_step"],
                overrides_json, 0, max_retries, priority,
                a.get("chunk_index"), a.get("chunk_size_frames"),
                now, current_user["uid"],
            ),
        )
        tasks.append({
            "job_id": job_id,
            "machine_id": a["machine_id"],
            "machine_gpu": a["gpu_model"],
            "machine_vram": a["gpu_vram_gb"],
            "frame_start": a["frame_start"],
            "frame_end": a["frame_end"],
            "frame_step": a["frame_step"],
            "total_frames": a["total_frames"],
            "rendered_frames": 0,
            "status": "pending",
        })

    # Dispatch serverless tasks in background
    from services import vast as vast_dispatch
    from services import runpod_dispatch

    overrides_b64 = base64.b64encode(overrides_json.encode()).decode()
    blend_url_base = (
        f"{runpod_dispatch.PUBLIC_BACKEND_URL}"
        f"/render-groups/{new_group_id}/input/{original['input_filename']}"
    )

    vast_strategy = get_strategy("vast_serverless")
    if vast_strategy.is_enabled():
        vast_blend_url = (
            f"{vast_dispatch.PUBLIC_BACKEND_URL}"
            f"/render-groups/{new_group_id}/input/{original['input_filename']}"
        )
        vast_tasks = [
            t for t in tasks if _machine_type_of(t["machine_id"]) == "vast_serverless"
        ]

        def _dispatch_vast(task: dict):
            try:
                coordinator.dispatch(
                    job_id=task["job_id"], machine_id=task["machine_id"],
                    machine_type="vast_serverless", blend_url=vast_blend_url,
                    frame_start=task["frame_start"], frame_end=task["frame_end"],
                    frame_step=task["frame_step"],
                    render_overrides_b64=overrides_b64, group_id=new_group_id,
                )
            except Exception as exc:
                log.error(f"Re-render dispatch failed for job {task['job_id']}: {exc}")
                execute(
                    "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                    (f"Dispatch failed: {exc}", now_iso(), task["job_id"]),
                )

        def _dispatch_vast_sequential():
            for task in vast_tasks:
                _dispatch_vast(task)
                time.sleep(2)

        if vast_tasks:
            threading.Thread(
                target=_dispatch_vast_sequential, daemon=True,
                name=f"vast-rerender-{new_group_id[:8]}",
            ).start()

    try:
        write_render_group_record(current_user["uid"], new_group_id, {
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
