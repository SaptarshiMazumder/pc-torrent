"""Render group routes — thin controllers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse

from serverV2.api.dependencies import get_current_user
from serverV2.api.schemas.render_group import (
    ConfirmRenderGroupPayload,
    CreateRenderGroupPayload,
)
from serverV2.api.schemas.upload import (
    MultipartAbortPayload,
    MultipartCompletePayload,
    MultipartInitPayload,
    MultipartPartUrlsPayload,
)
from serverV2.allocation.allocation_facade import AllocationFacade
from serverV2.orchestrator.allocation_client import AllocationClient
from serverV2.services.render_groups.service import RenderGroupService, RenderGroupServiceError
from serverV2.services.upload.coordinator import UploadCoordinator
from serverV2.services.upload.validators import UploadValidationError

router = APIRouter(tags=["render_groups"])

_svc: RenderGroupService | None = None
_upload: UploadCoordinator | None = None
_facade: AllocationFacade | None = None
_allocation_client: AllocationClient | None = None


def init(
    service: RenderGroupService,
    upload_coordinator: UploadCoordinator,
    allocation_facade: AllocationFacade,
    allocation_client: AllocationClient,
) -> None:
    global _svc, _upload, _facade, _allocation_client
    _svc = service
    _upload = upload_coordinator
    _facade = allocation_facade
    _allocation_client = allocation_client


def _get() -> RenderGroupService:
    if _svc is None:
        raise HTTPException(500, "RenderGroupService not initialized")
    return _svc


def _up() -> UploadCoordinator:
    if _upload is None:
        raise HTTPException(500, "UploadCoordinator not initialized")
    return _upload


def _get_facade() -> AllocationFacade:
    if _facade is None:
        raise HTTPException(500, "AllocationFacade not initialized")
    return _facade


def _get_allocation_client() -> AllocationClient:
    if _allocation_client is None:
        raise HTTPException(500, "AllocationClient not initialized")
    return _allocation_client


@router.get("/render-groups")
def list_render_groups(
    user: dict = Depends(get_current_user),
    limit: int = Query(5, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    return _get().list_with_status_page(user["uid"], limit=limit, offset=offset)


@router.post("/render-groups/create")
def create(payload: CreateRenderGroupPayload, user: dict = Depends(get_current_user)):
    try:
        return _get().create(payload, user)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)
    except UploadValidationError as e:
        raise HTTPException(e.status, e.message)


@router.post("/render-groups/{group_id}/confirm-upload")
def confirm_upload(group_id: str, payload: ConfirmRenderGroupPayload):
    try:
        return _get().confirm_upload(group_id, payload)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/render-groups/{group_id}")
def get_render_group(group_id: str):
    try:
        return _get().get_status(group_id)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/render-groups/{group_id}/status")
def get_status(group_id: str):
    try:
        return _get().get_status(group_id)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


@router.post("/render-groups/cancel-all")
def cancel_all():
    from serverV2.infrastructure.db import query_all
    groups = query_all(
        "SELECT id FROM render_groups WHERE status IN ('pending', 'running', 'uploading')"
    )
    total_cancelled = 0
    for g in groups:
        try:
            result = _get().cancel(g["id"])
            total_cancelled += result.get("cancelled_jobs", 0)
        except RenderGroupServiceError:
            pass
    return {"success": True, "cancelled_groups": len(groups), "cancelled_jobs": total_cancelled}


@router.post("/render-groups/{group_id}/cancel")
def cancel(group_id: str):
    try:
        return _get().cancel(group_id)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


@router.delete("/render-groups/{group_id}")
def delete(group_id: str, user: dict = Depends(get_current_user)):
    try:
        return _get().delete(group_id, user)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)



# ---- multipart upload ----

@router.post("/render-groups/{group_id}/multipart-upload/init")
def multipart_init(group_id: str, payload: MultipartInitPayload):
    try:
        key = _get().resolve_input_key(group_id)
        return _up().init_multipart(
            key, payload.file_size_bytes,
            payload.content_type, payload.part_size_bytes,
        )
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)
    except UploadValidationError as e:
        raise HTTPException(e.status, e.message)


@router.post("/render-groups/{group_id}/multipart-upload/part-urls")
def multipart_part_urls(group_id: str, payload: MultipartPartUrlsPayload):
    try:
        key = _get().resolve_input_key(group_id)
        return _up().part_urls(key, payload.upload_id, payload.part_numbers)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


@router.post("/render-groups/{group_id}/multipart-upload/complete")
def multipart_complete(group_id: str, payload: MultipartCompletePayload):
    try:
        key = _get().resolve_input_key(group_id)
        return _up().complete(key, payload.upload_id, payload.parts)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


@router.post("/render-groups/{group_id}/multipart-upload/abort")
def multipart_abort(group_id: str, payload: MultipartAbortPayload):
    try:
        key = _get().resolve_input_key(group_id)
        return _up().abort(key, payload.upload_id)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


# ---- input file download (workers pull blend from here) ----

@router.get("/render-groups/{group_id}/input/{filename}")
def download_input(group_id: str, filename: str):
    try:
        return RedirectResponse(_get().get_input_download_url(group_id))
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


# ---- output downloads ----

@router.get("/render-groups/{group_id}/outputs")
def get_outputs(group_id: str):
    try:
        return _get().get_outputs(group_id)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/render-groups/{group_id}/download-zip")
def download_zip(group_id: str):
    try:
        buf = _get().download_all_as_zip(group_id)
    except RenderGroupServiceError as e:
        raise HTTPException(e.status, e.message)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="group-{group_id[:8]}-output.zip"'},
    )


# ---------- pending allocation queue (per-group) ----------

@router.get("/render-groups/{group_id}/pending-queue")
def get_pending_queue(group_id: str):
    """Pending allocation rows for one group.  Detail-page polls this
    while frontend math says chunks are missing.  Backed by the Redis
    mirror; falls through to Postgres on a cold cache."""
    items = _get_allocation_client().list_pending_for_group(group_id)
    return {
        "items": [
            {
                "id": i.id,
                "type": i.type,
                "group_id": i.group_id,
                "chunk_index": i.chunk_index,
                "attempt": i.attempt,
                "frame_start": i.frame_start,
                "frame_end": i.frame_end,
                "frame_step": i.frame_step,
                "total_frames": i.total_frames,
                "engine": i.engine,
                "tier": i.tier,
                "max_retries": i.max_retries,
                "priority": i.priority,
                "input_filename": i.input_filename,
                "created_at": i.created_at,
                "last_attempted_at": i.last_attempted_at,
            }
            for i in items
        ],
    }


# ---------- cost ----------

@router.get("/render-groups/{group_id}/cost/estimate")
def cost_estimate(group_id: str):
    """Frozen-at-planning cost summary.  Sums the per-chunk estimates
    the AllocationPlanner stamped on every jobs row at planning time;
    static for the lifetime of the group.

    All-zero fields when the group has no jobs yet (still parked on
    pending_allocation_queue).  Caller can detect this via ``chunks ==
    0`` and show "Queued" in the UI.
    """
    estimate = _get_facade().cost_estimate_for_group(group_id)
    return {
        "chunks": estimate.chunks,
        "total_cost_usd": estimate.total_cost_usd,
        "total_seconds": estimate.total_seconds,
        "wall_time_seconds": estimate.wall_time_seconds,
    }
