"""Assets router — user input files (upload metadata, list, rename, delete)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serverV2.api.dependencies import get_current_user
from serverV2.api.schemas.render_group import UpdateInputFilePayload
from serverV2.services.assets.service import AssetService, AssetServiceError

router = APIRouter(tags=["assets"])

_svc: AssetService | None = None


def init(service: AssetService) -> None:
    global _svc
    _svc = service


def _get() -> AssetService:
    if _svc is None:
        raise HTTPException(500, "AssetService not initialized")
    return _svc


@router.get("/me/input-files")
def list_input_files(user: dict = Depends(get_current_user)):
    return _get().list_assets(user["uid"])


@router.get("/me/input-files/{asset_id}")
def get_input_file(asset_id: str, user: dict = Depends(get_current_user)):
    try:
        return _get().get_asset(asset_id, user["uid"])
    except AssetServiceError as e:
        raise HTTPException(e.status, e.message)


@router.patch("/me/input-files/{asset_id}")
def rename_input_file(asset_id: str, payload: UpdateInputFilePayload, user: dict = Depends(get_current_user)):
    try:
        return _get().rename_asset(asset_id, payload.display_name, user["uid"])
    except AssetServiceError as e:
        raise HTTPException(e.status, e.message)


@router.delete("/me/input-files/{asset_id}")
def delete_input_file(asset_id: str, user: dict = Depends(get_current_user)):
    try:
        return _get().delete_asset(asset_id, user["uid"])
    except AssetServiceError as e:
        raise HTTPException(e.status, e.message)


@router.post("/me/input-files/backfill")
def backfill_input_files(user: dict = Depends(get_current_user)):
    return _get().backfill(user["uid"])
