"""
Render group routes — thin HTTP layer that delegates to the service.

All business logic lives in:
  - services.render_groups.service  (create, confirm, cancel, rerender, get_status)
  - services.render_groups.upload_coordinator  (multipart upload flow)
  - scheduling.orchestrator  (dispatch planning + execution)
  - scheduling.failover_scanner  (background failover — no longer in GET handler)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.routers.jobs import (
    _sanitize_filename,
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
from models.value_objects import output_frame_sort_key
from firebase_auth import get_current_user
from infrastructure.db import query_all, query_one
import infrastructure.storage as storage
from services.render_groups.service import RenderGroupServiceError, render_group_service
from services.render_groups.upload_coordinator import UploadCoordinator, UploadError

router = APIRouter(tags=["render_groups"])
upload = UploadCoordinator()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_owner(group: dict[str, Any], current_user: dict) -> None:
    if not group.get("user_id") or group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")


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
# Routes — creation & upload
# ---------------------------------------------------------------------------

@router.post("/render-groups/create")
def create_render_group(
    payload: CreateRenderGroupPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return render_group_service.create(payload, current_user)
    except RenderGroupServiceError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.post("/render-groups/{group_id}/multipart-upload/init")
def init_render_group_multipart_upload(
    group_id: str,
    payload: MultipartInitPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return upload.init_multipart(
            group_id, current_user["uid"], payload.file_size_bytes,
            content_type=payload.content_type, part_size_bytes=payload.part_size_bytes,
        )
    except UploadError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.post("/render-groups/{group_id}/multipart-upload/part-urls")
def render_group_multipart_part_urls(
    group_id: str,
    payload: MultipartPartUrlsPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return upload.part_urls(group_id, current_user["uid"], payload.upload_id, payload.part_numbers)
    except UploadError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.post("/render-groups/{group_id}/multipart-upload/complete")
def complete_render_group_multipart_upload(
    group_id: str,
    payload: MultipartCompletePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return upload.complete(group_id, current_user["uid"], payload.upload_id, payload.parts)
    except UploadError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.post("/render-groups/{group_id}/multipart-upload/abort")
def abort_render_group_multipart_upload(
    group_id: str,
    payload: MultipartAbortPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return upload.abort(group_id, current_user["uid"], payload.upload_id)
    except UploadError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


# ---------------------------------------------------------------------------
# Routes — confirm upload & dispatch
# ---------------------------------------------------------------------------

@router.post("/render-groups/{group_id}/confirm-upload")
def confirm_render_group_upload(
    group_id: str,
    payload: ConfirmRenderGroupPayload,
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return render_group_service.confirm_upload(group_id, payload, current_user)
    except RenderGroupServiceError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


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
    return [render_group_service.get_status(g["id"]) for g in groups]


@router.get("/render-groups/{group_id}")
def get_render_group(
    group_id: str, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    group = query_one("SELECT user_id FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group.get("user_id") and group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    try:
        return render_group_service.get_status(group_id)
    except RenderGroupServiceError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.delete("/render-groups/{group_id}")
def delete_render_group(
    group_id: str, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    try:
        return render_group_service.delete(group_id, current_user)
    except RenderGroupServiceError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


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
    _ensure_owner(group, current_user)
    files = _build_render_group_output_entries(group_id)
    return {"group_id": group_id, "status": group.get("status"), "files": files, "count": len(files)}


@router.get("/render-groups/{group_id}/download")
def download_render_group_output(
    group_id: str, current_user: dict = Depends(get_current_user)
):
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    _ensure_owner(group, current_user)
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
    try:
        return render_group_service.cancel(group_id)
    except RenderGroupServiceError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


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
    try:
        return render_group_service.rerender(group_id, payload, current_user)
    except RenderGroupServiceError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
