"""Job routes — upload, status, progress, heartbeat, outputs, download."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse, StreamingResponse

from serverV2.api.dependencies import get_current_user
from serverV2.api.schemas.job import (
    JobHeartbeatPayload,
    RequestUploadPayload,
    UpdateJobProgressPayload,
    UpdateJobStatusPayload,
)
from serverV2.api.schemas.upload import (
    MultipartAbortPayload,
    MultipartCompletePayload,
    MultipartInitPayload,
    MultipartPartUrlsPayload,
)
from serverV2.services.jobs.service import JobService, JobServiceError
from serverV2.services.upload.coordinator import UploadCoordinator

router = APIRouter(tags=["jobs"])

_svc: JobService | None = None
_upload: UploadCoordinator | None = None


def init(service: JobService, upload_coordinator: UploadCoordinator) -> None:
    global _svc, _upload
    _svc = service
    _upload = upload_coordinator


def _get() -> JobService:
    if _svc is None:
        raise HTTPException(500, "JobService not initialized")
    return _svc


def _up() -> UploadCoordinator:
    if _upload is None:
        raise HTTPException(500, "UploadCoordinator not initialized")
    return _upload


# ---- upload flow ----

@router.post("/jobs/request-upload")
def request_upload(payload: RequestUploadPayload, user: dict = Depends(get_current_user)):
    try:
        return _get().request_upload(payload, user)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.post("/jobs/{job_id}/confirm-upload")
def confirm_upload(job_id: str, user: dict = Depends(get_current_user)):
    try:
        return _get().confirm_upload(job_id, user)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.post("/jobs/{job_id}/multipart-upload/init")
def multipart_init(job_id: str, payload: MultipartInitPayload):
    r2_key = f"jobs/{job_id}/input/file"
    try:
        return _up().init_multipart(r2_key, payload.file_size_bytes, payload.content_type, payload.part_size_bytes)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/jobs/{job_id}/multipart-upload/part-urls")
def multipart_part_urls(job_id: str, payload: MultipartPartUrlsPayload):
    r2_key = f"jobs/{job_id}/input/file"
    return _up().part_urls(r2_key, payload.upload_id, payload.part_numbers)


@router.post("/jobs/{job_id}/multipart-upload/complete")
def multipart_complete(job_id: str, payload: MultipartCompletePayload):
    r2_key = f"jobs/{job_id}/input/file"
    return _up().complete(r2_key, payload.upload_id, payload.parts)


@router.post("/jobs/{job_id}/multipart-upload/abort")
def multipart_abort(job_id: str, payload: MultipartAbortPayload):
    r2_key = f"jobs/{job_id}/input/file"
    return _up().abort(r2_key, payload.upload_id)


# ---- status callbacks ----

@router.put("/jobs/{job_id}/status")
def update_status(job_id: str, payload: UpdateJobStatusPayload):
    try:
        result = _get().update_status(job_id, payload.status, payload.error)
        if payload.output_files:
            _get().register_outputs(job_id, payload.output_files)
        return result
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.put("/jobs/{job_id}/progress")
def update_progress(job_id: str, payload: UpdateJobProgressPayload):
    try:
        return _get().update_progress(
            job_id, payload.rendered_frames, payload.total_frames or 0,
        )
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.put("/jobs/{job_id}/heartbeat")
def heartbeat(job_id: str, payload: JobHeartbeatPayload | None = None):
    phase = payload.phase if payload else None
    try:
        return _get().heartbeat(job_id, phase)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


# ---- reads ----

@router.get("/jobs")
def list_jobs(user: dict = Depends(get_current_user)):
    from serverV2.infrastructure.db import query_all
    from serverV2.services.jobs.serializers import serialize_job
    jobs = query_all(
        "SELECT * FROM jobs WHERE user_id = %s AND group_id IS NULL ORDER BY submitted_at DESC",
        (user["uid"],),
    )
    return [serialize_job(j) for j in jobs]


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    try:
        return _get().get_status(job_id)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/jobs/next-for-machine/{machine_id}")
def next_for_machine(machine_id: str):
    result = _get().next_for_machine(machine_id)
    if result is None:
        return {"job": None}
    return {"job": result}


# ---- output management ----

@router.post("/jobs/{job_id}/request-upload-urls")
def request_upload_urls(job_id: str, body: dict):
    from serverV2.infrastructure import storage
    from serverV2.core.value_objects import sanitize_filename
    from serverV2.infrastructure.db import query_one

    job = query_one("SELECT id, group_id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(404, "Job not found")
    filenames = body.get("filenames", [])
    if not filenames:
        raise HTTPException(400, "No filenames provided")

    group_id = job.get("group_id") or job_id
    urls = {
        fname: storage.generate_presigned_upload_url(
            f"jobs/{group_id}/output/{sanitize_filename(fname)}", expires_in=3600,
        )
        for fname in filenames
    }
    return {"urls": urls}


@router.get("/jobs/{job_id}/outputs")
def get_outputs(job_id: str):
    try:
        return _get().get_outputs(job_id)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/jobs/{job_id}/output/{filename}")
def download_output(job_id: str, filename: str):
    try:
        url = _get().get_output_download_url(job_id, filename)
        return RedirectResponse(url)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/jobs/{job_id}/output/{filename}/preview")
def preview_output(job_id: str, filename: str):
    try:
        payload, media_type = _get().get_output_preview(job_id, filename)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.get("/jobs/{job_id}/download-zip")
def download_zip(job_id: str):
    try:
        buf = _get().download_all_as_zip(job_id)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="job-{job_id[:8]}-output.zip"'},
        )
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, user: dict = Depends(get_current_user)):
    try:
        return _get().delete(job_id, user)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.post("/jobs/{job_id}/register-outputs")
def register_outputs(job_id: str, body: dict):
    filenames = body.get("filenames", [])
    if not filenames:
        raise HTTPException(400, "No filenames provided")
    try:
        return _get().register_outputs(job_id, filenames)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)
