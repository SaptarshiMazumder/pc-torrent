"""
User profile and saved input-file asset endpoints.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from api.schemas.render_group import UpdateInputFilePayload, UpdateProfilePayload
from models.value_objects import (
    now_iso,
    normalize_render_overrides,
    normalize_scheduling,
    parse_json_object,
)
from firebase_auth import (
    get_current_user,
    get_or_create_profile,
    get_user_profile,
    update_user_profile,
)
from infrastructure.db import execute, query_all, query_one
import infrastructure.storage as storage

router = APIRouter(tags=["assets"])


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _serialize_input_file_asset(row: dict[str, Any]) -> dict[str, Any]:
    frame_start = row.get("frame_start")
    frame_end = row.get("frame_end")
    frame_step = row.get("frame_step")
    prefill_frame_range = None
    if frame_start is not None and frame_end is not None:
        prefill_frame_range = {
            "frame_start": int(frame_start),
            "frame_end": int(frame_end),
            "frame_step": int(frame_step or 1),
        }

    analysis_snapshot = parse_json_object(row.get("analysis_snapshot_json"), {})
    render_overrides = normalize_render_overrides(
        parse_json_object(row.get("render_overrides_json"), {})
    )
    scheduling = normalize_scheduling(parse_json_object(row.get("scheduling_json"), {}))

    return {
        "id": row["id"],
        "display_name": row.get("display_name") or row.get("input_filename"),
        "input_filename": row.get("input_filename"),
        "r2_key": row.get("r2_key"),
        "size_bytes": row.get("size_bytes"),
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frame_step": frame_step,
        "analysis_snapshot": analysis_snapshot,
        "render_overrides": render_overrides,
        "scheduling": scheduling,
        "prefill_frame_range": prefill_frame_range,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_used_at": row.get("last_used_at"),
    }


# ---------------------------------------------------------------------------
# Shared DB helpers (used by render_groups router too)
# ---------------------------------------------------------------------------

def upsert_user_input_file(
    *,
    user_id: str,
    input_filename: str,
    r2_key: str,
    frame_start: int | None = None,
    frame_end: int | None = None,
    frame_step: int | None = None,
    analysis_snapshot: dict[str, Any] | None = None,
    render_overrides: dict[str, Any] | None = None,
    scheduling: dict[str, Any] | None = None,
    used_at: str | None = None,
) -> None:
    ts = used_at or now_iso()
    execute(
        """
        INSERT INTO user_input_files (
            id, user_id, display_name, input_filename, r2_key,
            frame_start, frame_end, frame_step,
            analysis_snapshot_json, render_overrides_json, scheduling_json,
            created_at, updated_at, last_used_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, r2_key) DO UPDATE
        SET input_filename = EXCLUDED.input_filename,
            frame_start = EXCLUDED.frame_start,
            frame_end = EXCLUDED.frame_end,
            frame_step = EXCLUDED.frame_step,
            analysis_snapshot_json = EXCLUDED.analysis_snapshot_json,
            render_overrides_json = EXCLUDED.render_overrides_json,
            scheduling_json = EXCLUDED.scheduling_json,
            updated_at = EXCLUDED.updated_at,
            last_used_at = EXCLUDED.last_used_at
        """,
        (
            str(uuid4()),
            user_id,
            input_filename,
            input_filename,
            r2_key,
            frame_start,
            frame_end,
            frame_step,
            json.dumps(analysis_snapshot or {}),
            json.dumps(render_overrides or {}),
            json.dumps(scheduling or {}),
            ts,
            ts,
            ts,
        ),
    )


def backfill_user_input_files(user_id: str) -> None:
    groups = query_all(
        """
        SELECT input_filename, r2_input_key, frame_start, frame_end, frame_step,
               analysis_snapshot_json, render_overrides_json, scheduling_json, submitted_at
        FROM render_groups
        WHERE user_id = %s
          AND status != 'uploading'
          AND r2_input_key IS NOT NULL
          AND r2_input_key != ''
        ORDER BY submitted_at DESC
        """,
        (user_id,),
    )
    for group in groups:
        upsert_user_input_file(
            user_id=user_id,
            input_filename=group.get("input_filename") or "input.blend",
            r2_key=group.get("r2_input_key") or "",
            frame_start=group.get("frame_start"),
            frame_end=group.get("frame_end"),
            frame_step=group.get("frame_step"),
            analysis_snapshot=parse_json_object(group.get("analysis_snapshot_json"), {}),
            render_overrides=parse_json_object(group.get("render_overrides_json"), {}),
            scheduling=parse_json_object(group.get("scheduling_json"), {}),
            used_at=group.get("submitted_at") or now_iso(),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)) -> dict:
    profile = get_or_create_profile(current_user["uid"], current_user.get("email"))
    return {"uid": current_user["uid"], **profile}


@router.put("/me")
def update_me(
    payload: UpdateProfilePayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    get_or_create_profile(current_user["uid"], current_user.get("email"))
    update_user_profile(current_user["uid"], updates)
    profile = get_user_profile(current_user["uid"]) or {}
    return {"uid": current_user["uid"], **profile}


@router.get("/me/input-files")
def list_my_input_files(
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    user_id = current_user["uid"]
    backfill_user_input_files(user_id)
    rows = query_all(
        """
        SELECT * FROM user_input_files
        WHERE user_id = %s
        ORDER BY updated_at DESC, created_at DESC
        """,
        (user_id,),
    )
    files = []
    for row in rows:
        payload = dict(row)
        r2_key = payload.get("r2_key")
        payload["size_bytes"] = storage.get_file_size(r2_key) if r2_key else None
        files.append(_serialize_input_file_asset(payload))
    return {"files": files, "count": len(files)}


@router.patch("/me/input-files/{asset_id}")
def rename_input_file(
    asset_id: str,
    payload: UpdateInputFilePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    asset = query_one(
        "SELECT * FROM user_input_files WHERE id = %s AND user_id = %s",
        (asset_id, current_user["uid"]),
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Saved input file not found")

    display_name = (payload.display_name or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name cannot be empty")
    if len(display_name) > 255:
        raise HTTPException(status_code=400, detail="display_name is too long (max 255 chars)")

    execute(
        "UPDATE user_input_files SET display_name = %s, updated_at = %s WHERE id = %s",
        (display_name, now_iso(), asset_id),
    )
    updated = query_one(
        "SELECT * FROM user_input_files WHERE id = %s AND user_id = %s",
        (asset_id, current_user["uid"]),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Saved input file not found")
    return _serialize_input_file_asset(updated)


@router.delete("/me/input-files/{asset_id}")
def delete_input_file(
    asset_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    asset = query_one(
        "SELECT * FROM user_input_files WHERE id = %s AND user_id = %s",
        (asset_id, current_user["uid"]),
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Saved input file not found")

    active_ref = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM render_groups
        WHERE r2_input_key = %s
          AND user_id = %s
          AND status IN ('uploading', 'pending', 'running')
        """,
        (asset["r2_key"], current_user["uid"]),
    )
    if (active_ref or {}).get("cnt", 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete while a render group is still uploading or rendering",
        )

    try:
        storage.delete_file(asset["r2_key"])
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete input file from storage: {exc}",
        )

    execute(
        "DELETE FROM user_input_files WHERE id = %s AND user_id = %s",
        (asset_id, current_user["uid"]),
    )
    return {"success": True, "deleted_id": asset_id}
