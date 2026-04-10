"""
Job routes: upload, status updates, output management, downloads.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from PIL import Image, UnidentifiedImageError

from api.schemas.job import (
    MultipartAbortPayload,
    MultipartCompletePayload,
    MultipartInitPayload,
    MultipartPartUrlsPayload,
    RequestUploadPayload,
    UpdateJobProgressPayload,
    UpdateJobStatusPayload,
)
from scheduling.frame_distributor import choose_retry_machine
from scheduling.dispatch_coordinator import coordinator
from domain.value_objects import (
    MAX_UPLOAD_BYTES,
    MULTIPART_DEFAULT_PART_SIZE_BYTES,
    MULTIPART_MAX_PARTS,
    MULTIPART_MIN_PART_SIZE_BYTES,
    PREVIEW_MAX_EDGE_PX,
    PREVIEW_WEBP_QUALITY,
    SINGLE_PUT_MAX_BYTES,
    compute_progress_pct,
    is_serverless,
    latest_output_filename,
    now_iso,
    output_frame_sort_key,
    parse_output_files,
)
from firebase_auth import get_current_user, write_job_record
from infrastructure.db import execute, query_one, query_all
import infrastructure.storage as storage

log = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    safe = Path(name).name
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return safe


def _validate_job_input_filename(name: str) -> None:
    ext = Path(name).suffix.lower()
    if ext not in {".blend", ".zip"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a .blend file or a .zip project bundle.",
        )


def _validate_upload_size(file_size_bytes: int) -> int:
    size = int(file_size_bytes or 0)
    if size <= 0:
        raise HTTPException(status_code=400, detail="file_size_bytes must be greater than zero")
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds max upload size of {MAX_UPLOAD_BYTES} bytes (100 GB)",
        )
    return size


def _choose_multipart_part_size(
    file_size_bytes: int,
    requested_part_size_bytes: int | None = None,
) -> int:
    size = _validate_upload_size(file_size_bytes)
    part_size = requested_part_size_bytes or MULTIPART_DEFAULT_PART_SIZE_BYTES
    part_size = max(int(part_size), MULTIPART_MIN_PART_SIZE_BYTES)
    required_min = math.ceil(size / MULTIPART_MAX_PARTS)
    part_size = max(part_size, required_min)
    mib = 1024 * 1024
    if part_size % mib != 0:
        part_size = ((part_size + mib - 1) // mib) * mib
    if math.ceil(size / part_size) > MULTIPART_MAX_PARTS:
        raise HTTPException(status_code=400, detail="Too many multipart chunks for this file size")
    return part_size


def _compute_total_parts(file_size_bytes: int, part_size_bytes: int) -> int:
    if part_size_bytes <= 0:
        raise HTTPException(status_code=400, detail="part_size_bytes must be greater than zero")
    return max(1, math.ceil(file_size_bytes / part_size_bytes))


def _normalize_part_numbers(part_numbers: list[int]) -> list[int]:
    if not part_numbers:
        raise HTTPException(status_code=400, detail="part_numbers cannot be empty")
    deduped = sorted({int(n) for n in part_numbers})
    if len(deduped) > 200:
        raise HTTPException(status_code=400, detail="Too many part numbers requested at once")
    for n in deduped:
        if n < 1 or n > MULTIPART_MAX_PARTS:
            raise HTTPException(status_code=400, detail=f"Invalid part number: {n}")
    return deduped


def _normalize_completed_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not parts:
        raise HTTPException(status_code=400, detail="parts cannot be empty")
    cleaned: list[dict[str, Any]] = []
    seen: set[int] = set()
    for part in parts:
        part_number = int(part.get("part_number", 0))
        etag = str(part.get("etag", "")).strip()
        if part_number < 1 or part_number > MULTIPART_MAX_PARTS:
            raise HTTPException(status_code=400, detail=f"Invalid part_number: {part_number}")
        if not etag:
            raise HTTPException(status_code=400, detail=f"Missing etag for part {part_number}")
        if part_number in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate part_number: {part_number}")
        seen.add(part_number)
        cleaned.append({"PartNumber": part_number, "ETag": etag})
    return cleaned


def _job_input_r2_key(job: dict[str, Any]) -> str:
    return f"jobs/{job['id']}/input/{job['input_filename']}"


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    output_files = parse_output_files(job.get("output_files"))
    total_frames = job.get("total_frames")
    rendered_frames = max(0, job.get("rendered_frames") or 0)
    if total_frames is not None and total_frames > 0:
        rendered_frames = min(rendered_frames, total_frames)
    return {
        **job,
        "output_files": output_files,
        "output_files_count": len(output_files),
        "latest_output_file": latest_output_filename(output_files),
        "total_frames": total_frames,
        "rendered_frames": rendered_frames,
        "progress_pct": compute_progress_pct(
            job.get("status", ""), rendered_frames, total_frames
        ),
    }


def build_job_output_entries(job: dict[str, Any]) -> list[dict[str, Any]]:
    files = parse_output_files(job.get("output_files"))
    entries: list[dict[str, Any]] = []
    for fname in sorted(files, key=output_frame_sort_key):
        try:
            safe_name = _sanitize_filename(fname)
        except HTTPException:
            continue
        r2_key = f"jobs/{job['id']}/output/{safe_name}"
        try:
            url = storage.generate_presigned_url(r2_key, download_name=safe_name)
            size_bytes = storage.get_file_size(r2_key)
        except Exception:
            continue
        entries.append({
            "job_id": job["id"],
            "filename": safe_name,
            "url": url,
            "preview_path": f"/jobs/{job['id']}/output/{quote(safe_name, safe='')}/preview",
            "size_bytes": size_bytes,
            "status": job.get("status"),
        })
    return entries


# ---------------------------------------------------------------------------
# Helpers shared with render_groups router
# ---------------------------------------------------------------------------

def _machine_type_of(machine_id: str) -> str:
    row = query_one("SELECT machine_type FROM machines WHERE id = %s", (machine_id,))
    return row["machine_type"] if row else "windows"


def _group_blocks_new_jobs(group_id: str) -> bool:
    row = query_one("SELECT status FROM render_groups WHERE id = %s", (group_id,))
    if not row:
        return True
    return row["status"] in {"cancelled", "failed", "done"}


# ---------------------------------------------------------------------------
# Upload routes
# ---------------------------------------------------------------------------

@router.post("/jobs/request-upload")
def request_upload(
    payload: RequestUploadPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    machine = query_one(
        "SELECT id FROM machines WHERE id = %s AND status = 'available'",
        (payload.machine_id,),
    )
    if not machine:
        raise HTTPException(status_code=400, detail="Machine not available")

    input_filename = _sanitize_filename(payload.filename)
    _validate_job_input_filename(input_filename)
    file_size_bytes = None
    multipart_required = False
    if payload.file_size_bytes is not None:
        file_size_bytes = _validate_upload_size(payload.file_size_bytes)
        multipart_required = file_size_bytes > SINGLE_PUT_MAX_BYTES

    job_id = str(uuid4())
    r2_key = f"jobs/{job_id}/input/{input_filename}"
    upload_url = storage.generate_presigned_upload_url(r2_key)

    execute(
        """
        INSERT INTO jobs (id, machine_id, input_filename, status, output_files, submitted_at, user_id)
        VALUES (%s, %s, %s, 'uploading', '[]', %s, %s)
        """,
        (job_id, payload.machine_id, input_filename, now_iso(), current_user["uid"]),
    )

    return {
        "job_id": job_id,
        "upload_url": upload_url,
        "r2_key": r2_key,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "single_put_max_bytes": SINGLE_PUT_MAX_BYTES,
        "multipart_required": multipart_required,
        "suggested_upload_mode": "multipart" if multipart_required else "single_put",
        "file_size_bytes": file_size_bytes,
    }


@router.post("/jobs/{job_id}/multipart-upload/init")
def init_job_multipart_upload(job_id: str, payload: MultipartInitPayload) -> dict[str, Any]:
    job = query_one("SELECT id, input_filename, status FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Job is not in uploading state")

    file_size_bytes = _validate_upload_size(payload.file_size_bytes)
    part_size_bytes = _choose_multipart_part_size(file_size_bytes, payload.part_size_bytes)
    total_parts = _compute_total_parts(file_size_bytes, part_size_bytes)
    r2_key = _job_input_r2_key(job)

    try:
        upload_id = storage.create_multipart_upload(
            r2_key, content_type=(payload.content_type or "application/octet-stream")
        )
    except Exception as exc:
        log.error("Failed to create multipart upload for job %s: %s", job_id, exc)
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


@router.post("/jobs/{job_id}/multipart-upload/part-urls")
def job_multipart_part_urls(
    job_id: str, payload: MultipartPartUrlsPayload
) -> dict[str, Any]:
    job = query_one("SELECT id, input_filename, status FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Job is not in uploading state")

    r2_key = _job_input_r2_key(job)
    numbers = _normalize_part_numbers(payload.part_numbers)
    urls = {
        str(n): storage.generate_presigned_upload_part_url(
            r2_key, payload.upload_id, n, expires_in=3600
        )
        for n in numbers
    }
    return {"upload_id": payload.upload_id, "urls": urls}


@router.post("/jobs/{job_id}/multipart-upload/complete")
def complete_job_multipart_upload(
    job_id: str, payload: MultipartCompletePayload
) -> dict[str, Any]:
    job = query_one("SELECT id, input_filename, status FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Job is not in uploading state")

    r2_key = _job_input_r2_key(job)
    parts = _normalize_completed_parts(payload.parts)
    try:
        result = storage.complete_multipart_upload(r2_key, payload.upload_id, parts)
    except Exception as exc:
        log.error("Failed to complete multipart upload for job %s: %s", job_id, exc)
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


@router.post("/jobs/{job_id}/multipart-upload/abort")
def abort_job_multipart_upload(
    job_id: str, payload: MultipartAbortPayload
) -> dict[str, Any]:
    job = query_one("SELECT id, input_filename, status FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    r2_key = _job_input_r2_key(job)
    try:
        storage.abort_multipart_upload(r2_key, payload.upload_id)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Failed to abort multipart upload: {exc}"
        )
    return {"success": True, "upload_id": payload.upload_id}


@router.post("/jobs/{job_id}/confirm-upload")
def confirm_upload(
    job_id: str, current_user: dict = Depends(get_current_user)
) -> dict[str, str]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if job["status"] != "uploading":
        raise HTTPException(status_code=400, detail="Job not in uploading state")

    r2_key = _job_input_r2_key(job)
    if not storage.file_exists(r2_key):
        raise HTTPException(
            status_code=400, detail="File not found in storage. Upload may have failed."
        )

    execute("UPDATE jobs SET status = 'pending' WHERE id = %s", (job_id,))
    execute(
        "UPDATE machines SET status = 'processing' WHERE id = %s", (job["machine_id"],)
    )

    try:
        write_job_record(current_user["uid"], job_id, {
            "job_id": job_id,
            "filename": job["input_filename"],
            "status": "pending",
            "machine_id": job["machine_id"],
            "submitted_at": job["submitted_at"],
        })
    except Exception:
        pass

    return {"job_id": job_id, "status": "pending"}


# ---------------------------------------------------------------------------
# Job polling (desktop agent)
# ---------------------------------------------------------------------------

@router.get("/jobs/next-for-machine/{machine_id}")
def get_next_job_for_machine(
    machine_id: str, request: Request
) -> dict[str, Any] | None:
    execute(
        "UPDATE machines SET last_seen_at = %s WHERE id = %s", (now_iso(), machine_id)
    )
    try:
        job = query_one(
            """
            SELECT * FROM jobs
            WHERE machine_id = %s AND status = 'pending'
            ORDER BY priority DESC, submitted_at ASC
            LIMIT 1
            """,
            (machine_id,),
        )
    except Exception:
        job = query_one(
            """
            SELECT * FROM jobs
            WHERE machine_id = %s AND status = 'pending'
            ORDER BY submitted_at ASC
            LIMIT 1
            """,
            (machine_id,),
        )
    if not job:
        return None

    base = str(request.base_url).rstrip("/")
    if job.get("group_id"):
        job["input_url"] = (
            f"{base}/render-groups/{job['group_id']}/input/{job['input_filename']}"
        )
    else:
        job["input_url"] = f"{base}/jobs/{job['id']}/input/{job['input_filename']}"
    return job


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------

@router.get("/jobs")
def list_jobs(
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    jobs = query_all(
        "SELECT * FROM jobs WHERE user_id = %s AND group_id IS NULL ORDER BY submitted_at DESC",
        (current_user["uid"],),
    )
    return [serialize_job(j) for j in jobs]


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    return serialize_job(job)


@router.put("/jobs/{job_id}/progress")
def update_job_progress(
    job_id: str, payload: UpdateJobProgressPayload
) -> dict[str, bool]:
    job = query_one(
        "SELECT id, status, total_frames, rendered_frames FROM jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "pending":
        # Worker sent progress before the 'running' status callback arrived (or it was dropped).
        # Auto-transition so we don't lose the frame counts.
        execute("UPDATE jobs SET status = 'running' WHERE id = %s", (job_id,))
        job = {**job, "status": "running"}
    if job["status"] != "running":
        raise HTTPException(status_code=409, detail="Job is not running")

    if payload.total_frames is not None and payload.total_frames < 0:
        raise HTTPException(status_code=400, detail="total_frames must be non-negative")
    if payload.rendered_frames < 0:
        raise HTTPException(status_code=400, detail="rendered_frames must be non-negative")

    current_total = job.get("total_frames")
    next_total = current_total
    if payload.total_frames is not None:
        next_total = max(current_total or 0, payload.total_frames)

    next_rendered = max(job.get("rendered_frames") or 0, payload.rendered_frames)
    if next_total and next_total > 0:
        next_rendered = min(next_rendered, next_total)

    execute(
        "UPDATE jobs SET total_frames = %s, rendered_frames = %s WHERE id = %s",
        (next_total, next_rendered, job_id),
    )
    return {"success": True}


@router.put("/jobs/{job_id}/status")
def update_job_status(
    job_id: str, payload: UpdateJobStatusPayload
) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    completed_at = job.get("completed_at")
    total_frames = job.get("total_frames")
    rendered_frames = max(0, job.get("rendered_frames") or 0)

    if payload.status in ("done", "failed"):
        completed_at = now_iso()
        if not is_serverless(_machine_type_of(job["machine_id"])):
            execute(
                "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
                (completed_at, job["machine_id"]),
            )
    if payload.status == "done" and total_frames and total_frames > 0:
        rendered_frames = total_frames

    output_files_json = job["output_files"]
    if payload.output_files is not None:
        output_files_json = json.dumps(payload.output_files)

    execute(
        """
        UPDATE jobs
        SET status = %s, completed_at = %s, error = %s,
            output_files = %s, rendered_frames = %s
        WHERE id = %s
        """,
        (payload.status, completed_at, payload.error,
         output_files_json, rendered_frames, job_id),
    )

    retry_job_id = None
    if payload.status == "failed" and job.get("group_id"):
        if not _group_blocks_new_jobs(job["group_id"]):
            retry_job_id = _schedule_retry(job, rendered_frames)

    if retry_job_id:
        return {"success": True, "retry_scheduled": True, "retry_job_id": retry_job_id}
    return {"success": True}


def _schedule_retry(job: dict[str, Any], rendered_frames: int) -> str | None:
    """Create a retry job for a failed group task and dispatch if serverless."""
    if _group_blocks_new_jobs(job["group_id"]):
        log.info(
            "Skipping retry creation for job %s because group %s is terminal",
            job["id"],
            job["group_id"],
        )
        return None

    step = job.get("frame_step") or 1
    remaining_start = job["frame_start"] + rendered_frames * step
    remaining_end = job["frame_end"]

    if remaining_start > remaining_end:
        return None

    retry_machine_id = (
        choose_retry_machine(job["group_id"], job["machine_id"])
        or job["machine_id"]
    )
    retry_job_id = str(uuid4())
    remaining_total = ((remaining_end - remaining_start) // step) + 1
    next_attempt = (job.get("attempt") or 0) + 1

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
            retry_job_id, retry_machine_id, job["group_id"],
            job["input_filename"], remaining_total,
            remaining_start, remaining_end, step,
            job.get("render_overrides_json") or "{}",
            next_attempt, job.get("max_retries") or 0,
            job.get("priority") or 0,
            job.get("chunk_index"), job.get("chunk_size_frames"),
            now_iso(),
        ),
    )

    retry_machine_type = _machine_type_of(retry_machine_id)
    if is_serverless(retry_machine_type):
        group = query_one(
            "SELECT * FROM render_groups WHERE id = %s", (job["group_id"],)
        )
        if group:
            from services import runpod_dispatch
            blend_url = (
                f"{runpod_dispatch.PUBLIC_BACKEND_URL}"
                f"/render-groups/{job['group_id']}/input/{group['input_filename']}"
            )
            overrides_b64 = base64.b64encode(
                (job.get("render_overrides_json") or "{}").encode()
            ).decode()
            try:
                coordinator.dispatch(
                    job_id=retry_job_id,
                    machine_id=retry_machine_id,
                    machine_type=retry_machine_type,
                    blend_url=blend_url,
                    frame_start=remaining_start,
                    frame_end=remaining_end,
                    frame_step=step,
                    render_overrides_b64=overrides_b64,
                    group_id=job["group_id"],
                )
            except Exception as exc:
                log.error(f"Retry dispatch failed for {retry_job_id}: {exc}")

    return retry_job_id


# ---------------------------------------------------------------------------
# Output management
# ---------------------------------------------------------------------------

@router.post("/jobs/{job_id}/request-upload-urls")
async def request_upload_urls(
    job_id: str, body: dict = Body(...)
) -> dict[str, Any]:
    job = query_one("SELECT id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    filenames: list[str] = body.get("filenames", [])
    if not filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    urls = {
        filename: storage.generate_presigned_upload_url(
            f"jobs/{job_id}/output/{_sanitize_filename(filename)}", expires_in=3600
        )
        for filename in filenames
    }
    return {"urls": urls}


@router.post("/jobs/{job_id}/register-outputs")
async def register_outputs(
    job_id: str, body: dict = Body(...)
) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    filenames: list[str] = body.get("filenames", [])
    safe_names = [_sanitize_filename(f) for f in filenames]
    existing = parse_output_files(job["output_files"])
    merged = list(dict.fromkeys(existing + safe_names))
    execute(
        "UPDATE jobs SET output_files = %s WHERE id = %s", (json.dumps(merged), job_id)
    )
    return {"success": True, "registered": len(safe_names)}


@router.post("/jobs/{job_id}/output")
async def upload_job_output(
    job_id: str, files: list[UploadFile] = File(...)
) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    uploaded_names: list[str] = []
    for file in files:
        filename = _sanitize_filename(file.filename or "output.bin")
        file_data = await file.read()
        storage.upload_file(f"jobs/{job_id}/output/{filename}", file_data)
        uploaded_names.append(filename)

    existing = parse_output_files(job["output_files"])
    merged = list(dict.fromkeys(existing + uploaded_names))
    execute(
        "UPDATE jobs SET output_files = %s WHERE id = %s", (json.dumps(merged), job_id)
    )
    return {"success": True, "files": merged}


# ---------------------------------------------------------------------------
# Download / preview routes
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id}/input/{filename}")
def download_job_input_file(job_id: str, filename: str):
    safe_name = _sanitize_filename(filename)
    r2_key = f"jobs/{job_id}/input/{safe_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")
    return RedirectResponse(url=storage.generate_presigned_url(r2_key, download_name=safe_name))


@router.get("/jobs/{job_id}/output/{filename}")
def download_job_output_file(
    job_id: str, filename: str, current_user: dict = Depends(get_current_user)
):
    job = query_one("SELECT id, user_id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")

    safe_name = _sanitize_filename(filename)
    r2_key = f"jobs/{job_id}/output/{safe_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")
    return RedirectResponse(url=storage.generate_presigned_url(r2_key, download_name=safe_name))


@router.get("/jobs/{job_id}/output/{filename}/preview")
def preview_job_output_file(
    job_id: str, filename: str, current_user: dict = Depends(get_current_user)
):
    job = query_one("SELECT id, user_id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")

    safe_name = _sanitize_filename(filename)
    r2_key = f"jobs/{job_id}/output/{safe_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        raw_image = storage.download_file(r2_key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        with Image.open(io.BytesIO(raw_image)) as image:
            if max(image.width, image.height) > PREVIEW_MAX_EDGE_PX:
                image.thumbnail(
                    (PREVIEW_MAX_EDGE_PX, PREVIEW_MAX_EDGE_PX),
                    Image.Resampling.LANCZOS,
                )
            has_alpha = "A" in image.getbands()
            image = image.convert("RGBA" if has_alpha else "RGB")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=PREVIEW_WEBP_QUALITY, method=6)
            payload_bytes = output.getvalue()
    except UnidentifiedImageError:
        raise HTTPException(status_code=415, detail="File is not previewable as an image")
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Failed to generate preview for %s/%s: %s", job_id, safe_name, exc)
        raise HTTPException(status_code=500, detail="Failed to generate image preview")

    return Response(
        content=payload_bytes,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.get("/jobs/{job_id}/outputs")
def list_job_outputs(
    job_id: str, current_user: dict = Depends(get_current_user)
):
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    entries = build_job_output_entries(job)
    return {"job_id": job_id, "status": job.get("status"), "files": entries, "count": len(entries)}


@router.get("/jobs/{job_id}/download")
def download_job_output_archive(
    job_id: str, current_user: dict = Depends(get_current_user)
):
    import zipfile

    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job not complete yet")

    files = parse_output_files(job["output_files"])
    if not files:
        raise HTTPException(status_code=404, detail="No output files found")

    if len(files) == 1:
        r2_key = f"jobs/{job_id}/output/{files[0]}"
        return RedirectResponse(url=storage.generate_presigned_url(r2_key, download_name=files[0]))

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename in files:
            try:
                data = storage.download_file(f"jobs/{job_id}/output/{filename}")
                zf.writestr(filename, data)
            except Exception:
                continue
    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=job_{job_id}_output.zip"},
    )
