import asyncio
import base64
import collections
import io
import json
import logging
import math
import os
import re
import threading
import time
import zipfile
from urllib.parse import quote

# Load .env before anything else reads os.environ
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from infrastructure.db import execute, init_db, query_all, query_one, request_conn
import infrastructure.storage as storage
import services.runpod_dispatch as runpod_dispatch
import services.modal_dispatch as modal_dispatch
from services.blend_parser import parse_upload, BlendParseError
from firebase_auth import get_current_user, get_or_create_profile, get_user_profile, update_user_profile, write_job_record, write_render_group_record
from fastapi import Depends

# ---------------------------------------------------------------------------
# In-memory log ring buffer + SSE broadcast
# ---------------------------------------------------------------------------
LOG_BUFFER_SIZE = 500
_log_buffer: collections.deque[str] = collections.deque(maxlen=LOG_BUFFER_SIZE)
_log_subscribers: list[asyncio.Queue] = []
_log_subscribers_lock = threading.Lock()


class _BroadcastHandler(logging.Handler):
    """Captures log records into a ring buffer and pushes to SSE subscribers."""

    def emit(self, record: logging.LogRecord):
        line = self.format(record)
        _log_buffer.append(line)
        with _log_subscribers_lock:
            for q in _log_subscribers:
                try:
                    q.put_nowait(line)
                except asyncio.QueueFull:
                    pass  # slow consumer, drop line


_broadcast_handler = _BroadcastHandler()
_broadcast_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s", datefmt="%H:%M:%S")
)
logging.root.addHandler(_broadcast_handler)
logging.root.setLevel(logging.INFO)

log = logging.getLogger(__name__)

MACHINE_STALE_SECONDS = 15
SERVERLESS_TYPES = {"runpod_serverless", "modal_serverless"}

def _is_serverless(machine_type: str) -> bool:
    return machine_type in SERVERLESS_TYPES
DEFAULT_DEVICE_POLICY = "AUTO"
ALLOWED_DEVICE_POLICIES = {"AUTO", "OPTIX", "CUDA", "CPU"}
ALLOWED_CAMERA_MODES = {"auto_markers", "force_camera", "camera_ranges"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024 * 1024  # 100 GB
SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 * 1024  # S3 PutObject hard limit
MULTIPART_MIN_PART_SIZE_BYTES = 5 * 1024 * 1024  # S3 minimum except last part
MULTIPART_DEFAULT_PART_SIZE_BYTES = 64 * 1024 * 1024
MULTIPART_MAX_PARTS = 10_000
PREVIEW_MAX_EDGE_PX = 512
PREVIEW_WEBP_QUALITY = 75

app = FastAPI(title="PC Rent Server")


@app.on_event("startup")
def _startup():
    init_db()
    runpod_dispatch.register_virtual_machines(execute, query_one, now_iso)
    runpod_dispatch.start_heartbeat_thread(execute, now_iso)
    modal_dispatch.register_virtual_machines(execute, query_one, now_iso)
    modal_dispatch.start_heartbeat_thread(execute, now_iso)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/logs/stream")
async def stream_logs(_: dict = Depends(get_current_user)):
    """SSE endpoint: streams application logs in real time."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=200)

    async def event_generator():
        # Send buffered history first
        for line in list(_log_buffer):
            yield {"data": line}
        # Then stream live
        with _log_subscribers_lock:
            _log_subscribers.append(q)
        try:
            while True:
                line = await q.get()
                yield {"data": line}
        except asyncio.CancelledError:
            pass
        finally:
            with _log_subscribers_lock:
                _log_subscribers.remove(q)

    return EventSourceResponse(event_generator())


@app.get("/logs/recent")
def recent_logs(_: dict = Depends(get_current_user)) -> list[str]:
    """Return the last N log lines as JSON array."""
    return list(_log_buffer)


class RegisterMachinePayload(BaseModel):
    machine_key: str | None = None
    gpu_model: str
    gpu_vram_gb: float
    cpu_cores: int
    ram_gb: float
    os_version: str | None = None
    nvidia_driver: str | None = None
    machine_type: str = "windows"  # "windows" or "linux_ssh"


class UpdateJobStatusPayload(BaseModel):
    status: str
    error: str | None = None
    output_files: list[str] | None = None


class UpdateJobProgressPayload(BaseModel):
    total_frames: int | None = None
    rendered_frames: int


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_output_files(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


_FRAME_INDEX_PATTERN = re.compile(r"(\d+)(?=\.[^.]+$)")


def output_frame_sort_key(filename: str) -> tuple[int, str]:
    """
    Sort frame files numerically when they contain a frame suffix (e.g. frame0007.png).
    Non-matching names are sorted after numbered frames.
    """
    if not isinstance(filename, str):
        return (10**12, "")
    match = _FRAME_INDEX_PATTERN.search(filename)
    if not match:
        return (10**12, filename.lower())
    try:
        frame_no = int(match.group(1))
    except ValueError:
        frame_no = 10**12
    return (frame_no, filename.lower())


def latest_output_filename(files: list[str]) -> str | None:
    if not files:
        return None
    return sorted(files, key=output_frame_sort_key)[-1]


def parse_json_object(raw: str | None, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not raw:
        return default.copy() if default else {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return default.copy() if default else {}


def parse_json_list(raw: str | None, default: list[Any] | None = None) -> list[Any]:
    if not raw:
        return list(default or [])
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    return list(default or [])


def _coerce_int(value: Any, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if minimum is not None and parsed < minimum:
        parsed = minimum
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed


def _coerce_float(value: Any, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if minimum is not None and parsed < minimum:
        parsed = minimum
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"1", "true", "yes", "on"}:
            return True
        if lower in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def normalize_render_overrides(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    timeline = src.get("timeline") if isinstance(src.get("timeline"), dict) else {}
    output = src.get("output") if isinstance(src.get("output"), dict) else {}
    render = src.get("render") if isinstance(src.get("render"), dict) else {}

    camera_mode_raw = src.get("camera_mode")
    camera_mode = camera_mode_raw if camera_mode_raw in ALLOWED_CAMERA_MODES else "auto_markers"

    device_policy_raw = render.get("device_policy")
    if isinstance(device_policy_raw, str):
        device_policy = device_policy_raw.strip().upper()
    else:
        device_policy = DEFAULT_DEVICE_POLICY
    if device_policy not in ALLOWED_DEVICE_POLICIES:
        device_policy = DEFAULT_DEVICE_POLICY

    camera_ranges_raw = src.get("camera_ranges")
    camera_ranges: list[dict[str, Any]] = []
    if isinstance(camera_ranges_raw, list):
        for item in camera_ranges_raw:
            if not isinstance(item, dict):
                continue
            camera_name = item.get("camera_name")
            if not isinstance(camera_name, str) or not camera_name.strip():
                continue
            frame_start = _coerce_int(item.get("frame_start"), minimum=1)
            frame_end = _coerce_int(item.get("frame_end"), minimum=1)
            if frame_start is None or frame_end is None or frame_end < frame_start:
                continue
            frame_step = _coerce_int(item.get("frame_step"), minimum=1) or 1
            camera_ranges.append(
                {
                    "camera_name": camera_name.strip(),
                    "frame_start": frame_start,
                    "frame_end": frame_end,
                    "frame_step": frame_step,
                    "enabled": _coerce_bool(item.get("enabled")) is not False,
                }
            )

    normalized = {
        "scene_name": src.get("scene_name") if isinstance(src.get("scene_name"), str) else None,
        "camera_mode": camera_mode,
        "camera_name": src.get("camera_name") if isinstance(src.get("camera_name"), str) else None,
        "camera_ranges": camera_ranges,
        "view_layer": src.get("view_layer") if isinstance(src.get("view_layer"), str) else None,
        "timeline": {
            "frame_start": _coerce_int(timeline.get("frame_start"), minimum=1),
            "frame_end": _coerce_int(timeline.get("frame_end"), minimum=1),
            "frame_step": _coerce_int(timeline.get("frame_step"), minimum=1),
            "fps": _coerce_float(timeline.get("fps"), minimum=1.0),
            "frame_map_old": _coerce_int(timeline.get("frame_map_old"), minimum=1),
            "frame_map_new": _coerce_int(timeline.get("frame_map_new"), minimum=1),
        },
        "output": {
            "path_pattern": output.get("path_pattern") if isinstance(output.get("path_pattern"), str) else None,
            "file_format": output.get("file_format") if isinstance(output.get("file_format"), str) else None,
            "color_mode": output.get("color_mode") if isinstance(output.get("color_mode"), str) else None,
            "color_depth": output.get("color_depth") if isinstance(output.get("color_depth"), str) else None,
            "compression": _coerce_int(output.get("compression"), minimum=0, maximum=100),
            "quality": _coerce_int(output.get("quality"), minimum=0, maximum=100),
            "exr_codec": output.get("exr_codec") if isinstance(output.get("exr_codec"), str) else None,
        },
        "render": {
            "engine": render.get("engine") if isinstance(render.get("engine"), str) else None,
            "resolution_x": _coerce_int(render.get("resolution_x"), minimum=1),
            "resolution_y": _coerce_int(render.get("resolution_y"), minimum=1),
            "resolution_percentage": _coerce_int(render.get("resolution_percentage"), minimum=1, maximum=1000),
            "cycles_samples": _coerce_int(render.get("cycles_samples"), minimum=1),
            "cycles_adaptive_sampling": _coerce_bool(render.get("cycles_adaptive_sampling")),
            "cycles_denoise": _coerce_bool(render.get("cycles_denoise")),
            "device_policy": device_policy,
        },
    }
    return normalized


def normalize_scheduling(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    return {
        "chunk_size_frames": _coerce_int(src.get("chunk_size_frames"), minimum=1),
        "max_retries_per_chunk": _coerce_int(src.get("max_retries_per_chunk"), minimum=0, maximum=10) if src.get("max_retries_per_chunk") is not None else 2,
        "priority": _coerce_int(src.get("priority"), minimum=-100, maximum=100) or 0,
    }


def extract_analysis_warnings(analysis_snapshot: dict[str, Any] | None) -> list[str]:
    if not isinstance(analysis_snapshot, dict):
        return []
    unsupported = analysis_snapshot.get("unsupported_fields")
    if not isinstance(unsupported, list):
        return []
    out = []
    for item in unsupported:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def compute_progress_pct(
    status: str,
    rendered_frames: int | None,
    total_frames: int | None,
) -> float | None:
    rendered = max(0, rendered_frames or 0)
    if total_frames and total_frames > 0:
        pct = rendered / total_frames * 100
        if status == "done":
            pct = 100.0
        return round(max(0.0, min(100.0, pct)), 1)
    if status == "done":
        return 100.0
    return None


def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    output_files = parse_output_files(job.get("output_files"))
    total_frames = job.get("total_frames")
    rendered_frames = max(0, job.get("rendered_frames") or 0)
    if total_frames is not None and total_frames > 0:
        rendered_frames = min(rendered_frames, total_frames)
    latest_output = latest_output_filename(output_files)

    return {
        **job,
        "output_files": output_files,
        "output_files_count": len(output_files),
        "latest_output_file": latest_output,
        "total_frames": total_frames,
        "rendered_frames": rendered_frames,
        "progress_pct": compute_progress_pct(
            job.get("status", ""),
            rendered_frames,
            total_frames,
        ),
    }


def sanitize_filename(name: str) -> str:
    from pathlib import Path
    safe = Path(name).name
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return safe


def validate_job_input_filename(name: str) -> None:
    from pathlib import Path
    ext = Path(name).suffix.lower()
    if ext not in {".blend", ".zip"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a .blend file or a .zip project bundle.",
        )


def validate_upload_size(file_size_bytes: int) -> int:
    size = int(file_size_bytes or 0)
    if size <= 0:
        raise HTTPException(status_code=400, detail="file_size_bytes must be greater than zero")
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds max upload size of {MAX_UPLOAD_BYTES} bytes (100 GB)",
        )
    return size


def choose_multipart_part_size(file_size_bytes: int, requested_part_size_bytes: int | None = None) -> int:
    size = validate_upload_size(file_size_bytes)

    part_size = requested_part_size_bytes or MULTIPART_DEFAULT_PART_SIZE_BYTES
    part_size = max(int(part_size), MULTIPART_MIN_PART_SIZE_BYTES)

    required_min = math.ceil(size / MULTIPART_MAX_PARTS)
    part_size = max(part_size, required_min)

    # Keep part boundaries aligned to 1 MiB for predictable client chunking.
    mib = 1024 * 1024
    if part_size % mib != 0:
        part_size = ((part_size + mib - 1) // mib) * mib

    total_parts = math.ceil(size / part_size)
    if total_parts > MULTIPART_MAX_PARTS:
        raise HTTPException(status_code=400, detail="Too many multipart chunks for this file size")

    return part_size


def compute_total_parts(file_size_bytes: int, part_size_bytes: int) -> int:
    if part_size_bytes <= 0:
        raise HTTPException(status_code=400, detail="part_size_bytes must be greater than zero")
    return max(1, math.ceil(file_size_bytes / part_size_bytes))


def normalize_part_numbers(part_numbers: list[int]) -> list[int]:
    if not part_numbers:
        raise HTTPException(status_code=400, detail="part_numbers cannot be empty")
    deduped = sorted({int(n) for n in part_numbers})
    if len(deduped) > 200:
        raise HTTPException(status_code=400, detail="Too many part numbers requested at once")
    for n in deduped:
        if n < 1 or n > MULTIPART_MAX_PARTS:
            raise HTTPException(status_code=400, detail=f"Invalid part number: {n}")
    return deduped


def normalize_completed_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def job_input_r2_key(job: dict[str, Any]) -> str:
    return f"jobs/{job['id']}/input/{job['input_filename']}"


def serialize_input_file_asset(row: dict[str, Any]) -> dict[str, Any]:
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


def ensure_render_group_owner(group: dict[str, Any], current_user: dict) -> None:
    if not group.get("user_id") or group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")


# -----------------------------------------------
# Health
# -----------------------------------------------
@app.get("/")
def root() -> dict[str, str]:
    return {"status": "PC Rent server running"}


# -----------------------------------------------
# User profile
# -----------------------------------------------
class UpdateProfilePayload(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None
    billing_plan: str | None = None


class UpdateInputFilePayload(BaseModel):
    display_name: str


@app.get("/me")
def get_me(current_user: dict = Depends(get_current_user)) -> dict:
    """Return the current user's Firestore profile (creates it on first access)."""
    profile = get_or_create_profile(current_user["uid"], current_user.get("email"))
    return {"uid": current_user["uid"], **profile}


@app.put("/me")
def update_me(payload: UpdateProfilePayload, current_user: dict = Depends(get_current_user)) -> dict:
    """Update editable profile fields."""
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    get_or_create_profile(current_user["uid"], current_user.get("email"))
    update_user_profile(current_user["uid"], updates)
    profile = get_user_profile(current_user["uid"]) or {}
    return {"uid": current_user["uid"], **profile}


@app.get("/me/input-files")
def list_my_input_files(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id = current_user["uid"]
    backfill_user_input_files(user_id)
    rows = query_all(
        """
        SELECT *
        FROM user_input_files
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
        files.append(serialize_input_file_asset(payload))
    return {"files": files, "count": len(files)}


@app.patch("/me/input-files/{asset_id}")
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
    return serialize_input_file_asset(updated)


@app.delete("/me/input-files/{asset_id}")
def delete_input_file(asset_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
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
        raise HTTPException(status_code=500, detail=f"Failed to delete input file from storage: {exc}")

    execute(
        "DELETE FROM user_input_files WHERE id = %s AND user_id = %s",
        (asset_id, current_user["uid"]),
    )
    return {"success": True, "deleted_id": asset_id}


# -----------------------------------------------
# Machines
# -----------------------------------------------
@app.post("/machines/register")
def register_machine(payload: RegisterMachinePayload, current_user: dict = Depends(get_current_user)) -> dict[str, str]:
    if not payload.gpu_model or payload.gpu_vram_gb is None:
        raise HTTPException(status_code=400, detail="Missing required fields")

    machine_key = payload.machine_key.strip() if payload.machine_key else None
    user_id = current_user["uid"]
    current_time = now_iso()

    existing = None
    if machine_key:
        existing = query_one(
            "SELECT id FROM machines WHERE machine_key = %s", (machine_key,)
        )

    if existing:
        machine_id = existing["id"]
        execute(
            """
            UPDATE machines
            SET machine_key = %s, gpu_model = %s, gpu_vram_gb = %s, cpu_cores = %s, ram_gb = %s,
                os_version = %s, nvidia_driver = %s, machine_type = %s,
                status = 'idle', registered_at = %s, last_seen_at = %s, user_id = %s
            WHERE id = %s
            """,
            (
                machine_key, payload.gpu_model, payload.gpu_vram_gb,
                payload.cpu_cores, payload.ram_gb,
                payload.os_version, payload.nvidia_driver, payload.machine_type,
                current_time, current_time, user_id, machine_id,
            ),
        )
    else:
        machine_id = str(uuid4())
        execute(
            """
            INSERT INTO machines (
                id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb,
                os_version, nvidia_driver, machine_type, status, registered_at, last_seen_at, user_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'idle', %s, %s, %s)
            """,
            (
                machine_id, machine_key, payload.gpu_model, payload.gpu_vram_gb,
                payload.cpu_cores, payload.ram_gb,
                payload.os_version, payload.nvidia_driver, payload.machine_type,
                current_time, current_time, user_id,
            ),
        )

    return {"machine_id": machine_id}


@app.put("/machines/{machine_id}/available")
def mark_machine_available(machine_id: str) -> dict[str, bool]:
    machine = query_one("SELECT id FROM machines WHERE id = %s", (machine_id,))
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    execute(
        "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    return {"success": True}


@app.put("/machines/{machine_id}/idle")
def mark_machine_idle(machine_id: str) -> dict[str, bool]:
    execute(
        "UPDATE machines SET status = 'idle', last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    return {"success": True}


@app.put("/machines/{machine_id}/heartbeat")
def heartbeat_machine(machine_id: str) -> dict[str, bool]:
    machine = query_one("SELECT id FROM machines WHERE id = %s", (machine_id,))
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    execute(
        "UPDATE machines SET last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    return {"success": True}


@app.get("/machines")
def list_available_machines(_: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    return query_available_machines()


def query_available_machines() -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=MACHINE_STALE_SECONDS)).isoformat()
    execute(
        """
        UPDATE machines
        SET status = 'idle'
        WHERE status = 'available'
          AND machine_type NOT IN ('runpod_serverless', 'modal_serverless')
          AND (last_seen_at IS NULL OR last_seen_at < %s)
        """,
        (cutoff,),
    )
    rows = query_all(
        """
        SELECT *
        FROM machines
        WHERE status = 'available' AND last_seen_at >= %s
        ORDER BY gpu_vram_gb DESC
        """,
        (cutoff,),
    )
    return rows


# -----------------------------------------------
# Jobs
# -----------------------------------------------
class RequestUploadPayload(BaseModel):
    machine_id: str
    filename: str
    file_size_bytes: int | None = None


class MultipartInitPayload(BaseModel):
    file_size_bytes: int
    content_type: str | None = "application/octet-stream"
    part_size_bytes: int | None = None


class MultipartPartUrlsPayload(BaseModel):
    upload_id: str
    part_numbers: list[int]


class MultipartCompletePayload(BaseModel):
    upload_id: str
    parts: list[dict[str, Any]]


class MultipartAbortPayload(BaseModel):
    upload_id: str


@app.post("/jobs/request-upload")
def request_upload(payload: RequestUploadPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Get a presigned URL to upload directly to R2."""
    machine = query_one(
        "SELECT id FROM machines WHERE id = %s AND status = 'available'",
        (payload.machine_id,),
    )
    if not machine:
        raise HTTPException(status_code=400, detail="Machine not available")

    input_filename = sanitize_filename(payload.filename)
    validate_job_input_filename(input_filename)
    file_size_bytes = None
    multipart_required = False
    if payload.file_size_bytes is not None:
        file_size_bytes = validate_upload_size(payload.file_size_bytes)
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


@app.post("/jobs/{job_id}/multipart-upload/init")
def init_job_multipart_upload(job_id: str, payload: MultipartInitPayload) -> dict[str, Any]:
    job = query_one(
        "SELECT id, input_filename, status FROM jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Job is not in uploading state")

    file_size_bytes = validate_upload_size(payload.file_size_bytes)
    part_size_bytes = choose_multipart_part_size(file_size_bytes, payload.part_size_bytes)
    total_parts = compute_total_parts(file_size_bytes, part_size_bytes)
    r2_key = job_input_r2_key(job)

    try:
        upload_id = storage.create_multipart_upload(
            r2_key,
            content_type=(payload.content_type or "application/octet-stream"),
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


@app.post("/jobs/{job_id}/multipart-upload/part-urls")
def job_multipart_part_urls(job_id: str, payload: MultipartPartUrlsPayload) -> dict[str, Any]:
    job = query_one(
        "SELECT id, input_filename, status FROM jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Job is not in uploading state")

    r2_key = job_input_r2_key(job)
    numbers = normalize_part_numbers(payload.part_numbers)
    urls: dict[str, str] = {}
    for n in numbers:
        urls[str(n)] = storage.generate_presigned_upload_part_url(
            r2_key,
            payload.upload_id,
            n,
            expires_in=3600,
        )
    return {"upload_id": payload.upload_id, "urls": urls}


@app.post("/jobs/{job_id}/multipart-upload/complete")
def complete_job_multipart_upload(job_id: str, payload: MultipartCompletePayload) -> dict[str, Any]:
    job = query_one(
        "SELECT id, input_filename, status FROM jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Job is not in uploading state")

    r2_key = job_input_r2_key(job)
    parts = normalize_completed_parts(payload.parts)
    try:
        result = storage.complete_multipart_upload(r2_key, payload.upload_id, parts)
    except Exception as exc:
        log.error("Failed to complete multipart upload for job %s: %s", job_id, exc)
        raise HTTPException(status_code=400, detail=f"Failed to complete multipart upload: {exc}")

    return {
        "success": True,
        "upload_id": payload.upload_id,
        "etag": result.get("ETag"),
        "location": result.get("Location"),
        "key": result.get("Key"),
    }


@app.post("/jobs/{job_id}/multipart-upload/abort")
def abort_job_multipart_upload(job_id: str, payload: MultipartAbortPayload) -> dict[str, Any]:
    job = query_one(
        "SELECT id, input_filename, status FROM jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    r2_key = job_input_r2_key(job)
    try:
        storage.abort_multipart_upload(r2_key, payload.upload_id)
    except Exception as exc:
        log.warning("Failed to abort multipart upload for job %s: %s", job_id, exc)
        raise HTTPException(status_code=400, detail=f"Failed to abort multipart upload: {exc}")

    return {"success": True, "upload_id": payload.upload_id}


@app.post("/jobs/{job_id}/confirm-upload")
def confirm_upload(job_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, str]:
    """Confirm the file was uploaded to R2 and start the job."""
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if job["status"] != "uploading":
        raise HTTPException(status_code=400, detail="Job not in uploading state")

    # Verify the file exists in R2
    r2_key = job_input_r2_key(job)
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=400, detail="File not found in storage. Upload may have failed.")

    execute("UPDATE jobs SET status = 'pending' WHERE id = %s", (job_id,))
    execute("UPDATE machines SET status = 'processing' WHERE id = %s", (job["machine_id"],))

    try:
        write_job_record(current_user["uid"], job_id, {
            "job_id": job_id,
            "filename": job["input_filename"],
            "status": "pending",
            "machine_id": job["machine_id"],
            "submitted_at": job["submitted_at"],
        })
    except Exception:
        pass  # Firestore write failure should not block the job

    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/next-for-machine/{machine_id}")
def get_next_job_for_machine(machine_id: str, request: Request) -> dict[str, Any] | None:
    execute(
        "UPDATE machines SET last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
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
        # Backward-compatibility fallback for older DB schemas that don't
        # yet have priority/chunking columns.
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

    # For render group jobs, the input file lives under the group's R2 path
    if job.get("group_id"):
        job["input_url"] = f"{base}/render-groups/{job['group_id']}/input/{job['input_filename']}"
    else:
        job["input_url"] = f"{base}/jobs/{job['id']}/input/{job['input_filename']}"

    return job


@app.get("/jobs")
def list_jobs(current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    jobs = query_all(
        "SELECT * FROM jobs WHERE user_id = %s AND group_id IS NULL ORDER BY submitted_at DESC",
        (current_user["uid"],),
    )
    return [serialize_job(j) for j in jobs]


@app.get("/jobs/{job_id}")
def get_job(job_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    return serialize_job(job)


@app.put("/jobs/{job_id}/progress")
def update_job_progress(job_id: str, payload: UpdateJobProgressPayload) -> dict[str, bool]:
    job = query_one(
        "SELECT id, status, total_frames, rendered_frames FROM jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "running":
        raise HTTPException(status_code=409, detail="Job is not running")

    current_total = job.get("total_frames")
    incoming_total = payload.total_frames
    if incoming_total is not None and incoming_total < 0:
        raise HTTPException(status_code=400, detail="total_frames must be non-negative")
    if payload.rendered_frames < 0:
        raise HTTPException(status_code=400, detail="rendered_frames must be non-negative")

    next_total = current_total
    if incoming_total is not None:
        next_total = max(current_total or 0, incoming_total)

    next_rendered = max(job.get("rendered_frames") or 0, payload.rendered_frames)
    if next_total and next_total > 0:
        next_rendered = min(next_rendered, next_total)

    execute(
        """
        UPDATE jobs
        SET total_frames = %s, rendered_frames = %s
        WHERE id = %s
        """,
        (next_total, next_rendered, job_id),
    )

    return {"success": True}


@app.put("/jobs/{job_id}/status")
def update_job_status(job_id: str, payload: UpdateJobStatusPayload) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    completed_at = job.get("completed_at")
    total_frames = job.get("total_frames")
    rendered_frames = max(0, job.get("rendered_frames") or 0)
    if payload.status in ("done", "failed"):
        completed_at = now_iso()
        # Serverless machines are always-available; heartbeat manages their last_seen_at
        if not _is_serverless(_machine_type_of(job["machine_id"])):
            execute(
                "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
                (completed_at, job["machine_id"]),
            )
    if payload.status == "done":
        if total_frames and total_frames > 0:
            rendered_frames = total_frames

    output_files_json = job["output_files"]
    if payload.output_files is not None:
        output_files_json = json.dumps(payload.output_files)

    execute(
        """
        UPDATE jobs
        SET status = %s, completed_at = %s, error = %s, output_files = %s,
            rendered_frames = %s
        WHERE id = %s
        """,
        (
            payload.status,
            completed_at,
            payload.error,
            output_files_json,
            rendered_frames,
            job_id,
        ),
    )

    retry_job_id = None
    group_status = None
    if job.get("group_id"):
        g = query_one("SELECT status FROM render_groups WHERE id = %s", (job["group_id"],))
        group_status = g["status"] if g else None
    if payload.status == "failed" and job.get("group_id") and group_status not in ("cancelled", "failed", "done"):
        # Calculate remaining frames (don't re-render what's already done)
        already_rendered = max(0, rendered_frames)
        step = job.get("frame_step") or 1
        remaining_start = job["frame_start"] + already_rendered * step
        remaining_end = job["frame_end"]

        if remaining_start <= remaining_end:
            retry_machine_id = choose_retry_machine(job["group_id"], job["machine_id"]) or job["machine_id"]
            retry_job_id = str(uuid4())
            remaining_total = ((remaining_end - remaining_start) // step) + 1
            next_attempt = (job.get("attempt") or 0) + 1
            execute(
                """
                INSERT INTO jobs (id, machine_id, group_id, input_filename, status,
                                  total_frames, rendered_frames, output_files,
                                  frame_start, frame_end, frame_step,
                                  render_overrides_json, attempt, max_retries, priority,
                                  chunk_index, chunk_size_frames, submitted_at)
                VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    retry_job_id,
                    retry_machine_id,
                    job["group_id"],
                    job["input_filename"],
                    remaining_total,
                    remaining_start,
                    remaining_end,
                    step,
                    job.get("render_overrides_json") or "{}",
                    next_attempt,
                    job.get("max_retries") or 0,
                    job.get("priority") or 0,
                    job.get("chunk_index"),
                    job.get("chunk_size_frames"),
                    now_iso(),
                ),
            )
            # If retry target is serverless, dispatch immediately
            retry_machine_type = _machine_type_of(retry_machine_id)
            if retry_machine_type == "runpod_serverless" and runpod_dispatch.is_enabled():
                group = query_one("SELECT * FROM render_groups WHERE id = %s", (job["group_id"],))
                if group:
                    blend_url = (
                        f"{runpod_dispatch.PUBLIC_BACKEND_URL}"
                        f"/render-groups/{job['group_id']}/input/{group['input_filename']}"
                    )
                    overrides_b64 = base64.b64encode(
                        (job.get("render_overrides_json") or "{}").encode()
                    ).decode()
                    try:
                        rp_job_id = runpod_dispatch.dispatch_and_save(
                            job_id=retry_job_id,
                            blend_url=blend_url,
                            frame_start=remaining_start,
                            frame_end=remaining_end,
                            frame_step=step,
                            render_overrides_b64=overrides_b64,
                            machine_id=retry_machine_id,
                            db_execute=execute,
                        )
                        runpod_dispatch.start_polling_thread(
                            job_id=retry_job_id,
                            runpod_job_id=rp_job_id,
                            db_execute=execute,
                            db_query_one=query_one,
                            now_iso=now_iso,
                            machine_id=retry_machine_id,
                            blend_url=blend_url,
                            render_overrides_b64=overrides_b64,
                            db_query_all=query_all,
                            group_id=job["group_id"],
                        )
                    except Exception as exc:
                        log.error(f"Retry dispatch to RunPod failed for {retry_job_id}: {exc}")
            elif retry_machine_type == "modal_serverless" and modal_dispatch.is_enabled():
                group = query_one("SELECT * FROM render_groups WHERE id = %s", (job["group_id"],))
                if group:
                    blend_url = (
                        f"{modal_dispatch.PUBLIC_BACKEND_URL}"
                        f"/render-groups/{job['group_id']}/input/{group['input_filename']}"
                    )
                    overrides_b64 = base64.b64encode(
                        (job.get("render_overrides_json") or "{}").encode()
                    ).decode()
                    try:
                        modal_job_id = modal_dispatch.dispatch_and_save(
                            job_id=retry_job_id,
                            blend_url=blend_url,
                            frame_start=remaining_start,
                            frame_end=remaining_end,
                            frame_step=step,
                            render_overrides_b64=overrides_b64,
                            machine_id=retry_machine_id,
                            db_execute=execute,
                        )
                        modal_dispatch.start_monitoring_thread(
                            job_id=retry_job_id,
                            provider_job_id=modal_job_id,
                            db_execute=execute,
                            db_query_one=query_one,
                            now_iso=now_iso,
                            machine_id=retry_machine_id,
                            blend_url=blend_url,
                            render_overrides_b64=overrides_b64,
                            db_query_all=query_all,
                            group_id=job["group_id"],
                        )
                    except Exception as exc:
                        log.error(f"Retry dispatch to Modal failed for {retry_job_id}: {exc}")

    if retry_job_id and payload.status == "failed":
        return {"success": True, "retry_scheduled": True, "retry_job_id": retry_job_id}

    return {"success": True}


@app.post("/jobs/{job_id}/request-upload-urls")
async def request_upload_urls(job_id: str, body: dict = Body(...)) -> dict[str, Any]:
    """Return presigned R2 PUT URLs so RunPod workers can upload frames directly (bypasses Cloud Run size limit)."""
    job = query_one("SELECT id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    filenames: list[str] = body.get("filenames", [])
    if not filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    urls = {}
    for filename in filenames:
        safe_name = sanitize_filename(filename)
        r2_key = f"jobs/{job_id}/output/{safe_name}"
        urls[filename] = storage.generate_presigned_upload_url(r2_key, expires_in=3600)
    return {"urls": urls}


@app.post("/jobs/{job_id}/register-outputs")
async def register_outputs(job_id: str, body: dict = Body(...)) -> dict[str, Any]:
    """Register filenames already uploaded directly to R2 (updates DB output_files list)."""
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    filenames: list[str] = body.get("filenames", [])
    safe_names = [sanitize_filename(f) for f in filenames]
    existing = parse_output_files(job["output_files"])
    merged = list(dict.fromkeys(existing + safe_names))
    execute("UPDATE jobs SET output_files = %s WHERE id = %s", (json.dumps(merged), job_id))
    return {"success": True, "registered": len(safe_names)}


@app.post("/jobs/{job_id}/output")
async def upload_job_output(job_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    uploaded_names: list[str] = []
    for file in files:
        filename = sanitize_filename(file.filename or "output.bin")
        file_data = await file.read()
        r2_key = f"jobs/{job_id}/output/{filename}"
        storage.upload_file(r2_key, file_data)
        uploaded_names.append(filename)

    existing = parse_output_files(job["output_files"])
    merged = list(dict.fromkeys(existing + uploaded_names))

    execute(
        "UPDATE jobs SET output_files = %s WHERE id = %s",
        (json.dumps(merged), job_id),
    )

    return {"success": True, "files": merged}


@app.get("/jobs/{job_id}/input/{filename}")
def download_job_input_file(job_id: str, filename: str):
    safe_name = sanitize_filename(filename)
    r2_key = f"jobs/{job_id}/input/{safe_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")
    url = storage.generate_presigned_url(r2_key, download_name=safe_name)
    return RedirectResponse(url=url)


@app.get("/render-groups/{group_id}/input/{filename}")
def download_render_group_input_file(group_id: str, filename: str):
    """Serve input file for render group jobs (shared across all tasks)."""
    group = query_one(
        "SELECT input_filename, r2_input_key FROM render_groups WHERE id = %s",
        (group_id,),
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    safe_name = sanitize_filename(filename)
    canonical_name = sanitize_filename(group.get("input_filename") or safe_name)
    if safe_name != canonical_name:
        raise HTTPException(status_code=404, detail="File not found")
    r2_key = group.get("r2_input_key") or f"jobs/{group_id}/input/{canonical_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")
    url = storage.generate_presigned_url(r2_key, download_name=canonical_name)
    return RedirectResponse(url=url)


@app.get("/jobs/{job_id}/output/{filename}")
def download_job_output_file(job_id: str, filename: str, current_user: dict = Depends(get_current_user)):
    job = query_one("SELECT id, user_id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")

    safe_name = sanitize_filename(filename)
    r2_key = f"jobs/{job_id}/output/{safe_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")
    url = storage.generate_presigned_url(r2_key, download_name=safe_name)
    return RedirectResponse(url=url)


@app.get("/jobs/{job_id}/output/{filename}/preview")
def preview_job_output_file(job_id: str, filename: str, current_user: dict = Depends(get_current_user)):
    job = query_one("SELECT id, user_id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")

    safe_name = sanitize_filename(filename)
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
                image.thumbnail((PREVIEW_MAX_EDGE_PX, PREVIEW_MAX_EDGE_PX), Image.Resampling.LANCZOS)

            has_alpha = "A" in image.getbands()
            image = image.convert("RGBA" if has_alpha else "RGB")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=PREVIEW_WEBP_QUALITY, method=6)
            payload = output.getvalue()
    except UnidentifiedImageError:
        raise HTTPException(status_code=415, detail="File is not previewable as an image")
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Failed to generate preview for %s/%s: %s", job_id, safe_name, exc)
        raise HTTPException(status_code=500, detail="Failed to generate image preview")

    return Response(
        content=payload,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=60"},
    )


def build_job_output_entries(job: dict[str, Any]) -> list[dict[str, Any]]:
    files = parse_output_files(job.get("output_files"))
    ordered_files = sorted(files, key=output_frame_sort_key)
    entries: list[dict[str, Any]] = []
    for fname in ordered_files:
        try:
            safe_name = sanitize_filename(fname)
        except HTTPException:
            continue
        r2_key = f"jobs/{job['id']}/output/{safe_name}"
        try:
            url = storage.generate_presigned_url(r2_key, download_name=safe_name)
            size_bytes = storage.get_file_size(r2_key)
        except Exception:
            continue
        entries.append(
            {
                "job_id": job["id"],
                "filename": safe_name,
                "url": url,
                "preview_path": f"/jobs/{job['id']}/output/{quote(safe_name, safe='')}/preview",
                "size_bytes": size_bytes,
                "status": job.get("status"),
            }
        )
    return entries


@app.get("/jobs/{job_id}/outputs")
def list_job_outputs(job_id: str, current_user: dict = Depends(get_current_user)):
    """
    List all currently available output files for a job.
    Works for running/failed/done jobs, enabling partial frame recovery.
    """
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") and job["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    entries = build_job_output_entries(job)
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "files": entries,
        "count": len(entries),
    }


@app.get("/jobs/{job_id}/download")
def download_job_output_archive(job_id: str, current_user: dict = Depends(get_current_user)):
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

    # Single file â†’ redirect to presigned URL
    if len(files) == 1:
        r2_key = f"jobs/{job_id}/output/{files[0]}"
        url = storage.generate_presigned_url(r2_key, download_name=files[0])
        return RedirectResponse(url=url)

    # Multiple files â†’ zip them in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename in files:
            r2_key = f"jobs/{job_id}/output/{filename}"
            try:
                data = storage.download_file(r2_key)
                zf.writestr(filename, data)
            except Exception:
                continue
    zip_buffer.seek(0)

    headers = {"Content-Disposition": f"attachment; filename=job_{job_id}_output.zip"}
    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)


# -----------------------------------------------
# Render Groups (distributed multi-machine rendering)
# -----------------------------------------------

FAILOVER_STALE_SECONDS = 30  # longer than heartbeat to avoid false positives
MIN_FRAMES_PER_WORKER = 2


def compute_power_score(machine: dict) -> float:
    """Compute a rendering power score from machine specs (GPU-weighted)."""
    vram = machine.get("gpu_vram_gb") or 0
    cores = machine.get("cpu_cores") or 0
    ram = machine.get("ram_gb") or 0
    return (vram * 4) + (cores * 1) + (ram * 0.3)


def max_workers_for_frame_budget(total_frames: int, requested_workers: int) -> int:
    """Cap workers so each gets at least MIN_FRAMES_PER_WORKER frames."""
    if requested_workers <= 1:
        return 1
    if total_frames <= 0:
        return 1
    max_workers_by_frames = max(1, total_frames // MIN_FRAMES_PER_WORKER)
    return max(1, min(requested_workers, max_workers_by_frames))


def limit_machines_for_frame_budget(
    machines: list[dict],
    total_frames: int,
) -> list[dict]:
    """Use only as many machines as the frame budget can justify."""
    if not machines:
        return []
    allowed = max_workers_for_frame_budget(total_frames, len(machines))
    if allowed >= len(machines):
        return machines
    ranked = sorted(machines, key=compute_power_score, reverse=True)
    return ranked[:allowed]


def distribute_frames(
    total_frames: int,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    machines: list[dict],
) -> list[dict]:
    """Split frames across machines proportionally to their power scores."""
    machines = limit_machines_for_frame_budget(machines, total_frames)
    if not machines:
        return []

    scores = [(m, compute_power_score(m)) for m in machines]
    total_score = sum(s for _, s in scores)
    if total_score <= 0:
        total_score = len(machines)
        scores = [(m, 1.0) for m in machines]

    assignments = []
    current_frame = frame_start

    for i, (machine, score) in enumerate(scores):
        if i == len(scores) - 1:
            chunk_end = frame_end
        else:
            share = score / total_score
            chunk_frames = max(1, round(total_frames * share))
            chunk_end = current_frame + (chunk_frames - 1) * frame_step
            chunk_end = min(chunk_end, frame_end)

        chunk_total = ((chunk_end - current_frame) // frame_step) + 1 if chunk_end >= current_frame else 0
        assignments.append({
            "machine_id": machine["id"],
            "machine_type": machine.get("machine_type", "windows"),
            "gpu_model": machine.get("gpu_model", "Unknown"),
            "gpu_vram_gb": machine.get("gpu_vram_gb", 0),
            "cpu_cores": machine.get("cpu_cores", 0),
            "ram_gb": machine.get("ram_gb", 0),
            "frame_start": current_frame,
            "frame_end": chunk_end,
            "frame_step": frame_step,
            "total_frames": chunk_total,
            "power_score": round(score, 1),
        })
        current_frame = chunk_end + frame_step

    return assignments


try:
    WORKERS_PER_SERVERLESS = max(1, int(os.getenv("WORKERS_PER_SERVERLESS", "3")))
except ValueError:
    WORKERS_PER_SERVERLESS = 3


def expand_serverless_assignments(
    assignments: list[dict],
    workers_per_endpoint: int = WORKERS_PER_SERVERLESS,
) -> list[dict]:
    """Split each serverless assignment into multiple sub-assignments for parallel workers."""
    expanded: list[dict] = []
    for a in assignments:
        mt = a.get("machine_type", "")
        if not _is_serverless(mt):
            expanded.append(a)
            continue
        # Use provider-specific worker count
        if mt == "modal_serverless":
            effective_workers = modal_dispatch.MODAL_WORKERS_PER_ENDPOINT
        else:
            effective_workers = workers_per_endpoint
        if effective_workers <= 1:
            expanded.append(a)
            continue

        frame_start = a["frame_start"]
        frame_end = a["frame_end"]
        frame_step = a["frame_step"]
        total_frames = a["total_frames"]

        if total_frames <= 1:
            expanded.append(a)
            continue

        worker_count = max_workers_for_frame_budget(total_frames, effective_workers)
        if worker_count <= 1:
            expanded.append(a)
            continue

        frames_per_worker = max(1, total_frames // worker_count)
        current = frame_start
        for w in range(worker_count):
            if current > frame_end:
                break
            if w == worker_count - 1:
                sub_end = frame_end
            else:
                sub_end = current + (frames_per_worker - 1) * frame_step
                sub_end = min(sub_end, frame_end)

            sub_total = ((sub_end - current) // frame_step) + 1 if sub_end >= current else 0
            if sub_total <= 0:
                break

            sub = dict(a)
            sub["frame_start"] = current
            sub["frame_end"] = sub_end
            sub["total_frames"] = sub_total
            expanded.append(sub)

            current = sub_end + frame_step

    return expanded


def distribute_frames_by_chunk_size(
    frame_start: int,
    frame_end: int,
    frame_step: int,
    machines: list[dict],
    chunk_size_frames: int,
) -> list[dict]:
    """Split frame range into fixed-size chunks and assign in power-ranked round-robin."""
    if chunk_size_frames < 1:
        raise ValueError("chunk_size_frames must be >= 1")
    effective_chunk_size = max(chunk_size_frames, MIN_FRAMES_PER_WORKER)
    total_frames = ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0
    machines = limit_machines_for_frame_budget(machines, total_frames)
    if not machines:
        return []

    ranked = sorted(
        [(m, compute_power_score(m)) for m in machines],
        key=lambda item: item[1],
        reverse=True,
    )
    if not ranked:
        return []

    assignments = []
    current_frame = frame_start
    chunk_index = 0
    while current_frame <= frame_end:
        machine, score = ranked[chunk_index % len(ranked)]
        frames_remaining = ((frame_end - current_frame) // frame_step) + 1
        if frames_remaining <= effective_chunk_size:
            frames_for_chunk = frames_remaining
        else:
            frames_for_chunk = effective_chunk_size
            tail_frames = frames_remaining - frames_for_chunk
            if 0 < tail_frames < MIN_FRAMES_PER_WORKER:
                # Avoid creating a tiny tail chunk on the next iteration.
                frames_for_chunk = frames_remaining

        chunk_end = current_frame + (frames_for_chunk - 1) * frame_step
        chunk_end = min(chunk_end, frame_end)
        chunk_total = ((chunk_end - current_frame) // frame_step) + 1 if chunk_end >= current_frame else 0
        assignments.append({
            "machine_id": machine["id"],
            "machine_type": machine.get("machine_type", "windows"),
            "gpu_model": machine.get("gpu_model", "Unknown"),
            "gpu_vram_gb": machine.get("gpu_vram_gb", 0),
            "cpu_cores": machine.get("cpu_cores", 0),
            "ram_gb": machine.get("ram_gb", 0),
            "frame_start": current_frame,
            "frame_end": chunk_end,
            "frame_step": frame_step,
            "total_frames": chunk_total,
            "power_score": round(score, 1),
            "chunk_index": chunk_index,
            "chunk_size_frames": effective_chunk_size,
        })
        current_frame = chunk_end + frame_step
        chunk_index += 1

    return assignments


def serialize_render_group_task(job: dict, machine: dict | None = None) -> dict:
    """Serialize a job within a render group into a task dict for the API."""
    total_frames = job.get("total_frames")
    rendered_frames = max(0, job.get("rendered_frames") or 0)
    if total_frames and total_frames > 0:
        rendered_frames = min(rendered_frames, total_frames)
    output_files = parse_output_files(job.get("output_files"))
    latest_output = latest_output_filename(output_files)

    return {
        "job_id": job["id"],
        "runpod_job_id": job.get("runpod_job_id"),
        "machine_id": job["machine_id"],
        "machine_gpu": machine["gpu_model"] if machine else "Unknown",
        "machine_vram": machine.get("gpu_vram_gb", 0) if machine else 0,
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
        "latest_output_file": latest_output,
    }


def _check_failover(group_id: str, tasks_raw: list[dict]):
    """Check for stale machines in a render group and reassign their work.

    Called during polling. Mutates the database if reassignment happens.
    Returns list of any newly created job IDs.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=FAILOVER_STALE_SECONDS)).isoformat()
    new_job_ids = []

    # Collect machines in this group for power scoring
    machine_ids_in_group = list({t["machine_id"] for t in tasks_raw})
    machines_map = {}
    for mid in machine_ids_in_group:
        m = query_one("SELECT * FROM machines WHERE id = %s", (mid,))
        if m:
            machines_map[mid] = m

    for task in tasks_raw:
        if task["status"] != "running":
            continue

        machine = machines_map.get(task["machine_id"])
        if not machine:
            continue

        last_seen = machine.get("last_seen_at") or ""
        if last_seen >= cutoff:
            continue

        # Machine is stale â€” mark job as failed
        execute(
            "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
            ("Machine went offline", now_iso(), task["id"]),
        )

        # Calculate remaining frames
        rendered = max(0, task.get("rendered_frames") or 0)
        step = task.get("frame_step") or 1
        new_start = task["frame_start"] + rendered * step
        new_end = task["frame_end"]

        if new_start > new_end:
            continue  # all frames were already rendered

        # Find the best available machine from the current available pool
        best_machine_id = choose_retry_machine(group_id, task["machine_id"])
        if not best_machine_id or best_machine_id == task["machine_id"]:
            continue
        best_machine = query_one("SELECT * FROM machines WHERE id = %s", (best_machine_id,))
        if not best_machine:
            continue

        # Create reassignment job
        new_job_id = str(uuid4())
        new_total = ((new_end - new_start) // step) + 1
        execute(
            """
            INSERT INTO jobs (id, machine_id, group_id, input_filename, status,
                              total_frames, rendered_frames, output_files,
                              frame_start, frame_end, frame_step,
                              render_overrides_json, attempt, max_retries, priority,
                              chunk_index, chunk_size_frames, submitted_at)
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_job_id, best_machine["id"], group_id,
                task["input_filename"], new_total,
                new_start, new_end, step,
                task.get("render_overrides_json") or "{}",
                task.get("attempt") or 0,
                task.get("max_retries") or 0,
                task.get("priority") or 0,
                task.get("chunk_index"),
                task.get("chunk_size_frames"),
                now_iso(),
            ),
        )
        new_job_ids.append(new_job_id)

        # If reassigned to serverless, dispatch immediately
        if best_machine.get("machine_type") == "runpod_serverless" and runpod_dispatch.is_enabled():
            group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
            if group:
                blend_url = (
                    f"{runpod_dispatch.PUBLIC_BACKEND_URL}"
                    f"/render-groups/{group_id}/input/{group['input_filename']}"
                )
                overrides_b64 = base64.b64encode(
                    (task.get("render_overrides_json") or "{}").encode()
                ).decode()
                try:
                    rp_job_id = runpod_dispatch.dispatch_and_save(
                        job_id=new_job_id,
                        blend_url=blend_url,
                        frame_start=new_start,
                        frame_end=new_end,
                        frame_step=step,
                        render_overrides_b64=overrides_b64,
                        machine_id=best_machine["id"],
                        db_execute=execute,
                    )
                    runpod_dispatch.start_polling_thread(
                        job_id=new_job_id,
                        runpod_job_id=rp_job_id,
                        db_execute=execute,
                        db_query_one=query_one,
                        now_iso=now_iso,
                        machine_id=best_machine["id"],
                        blend_url=blend_url,
                        render_overrides_b64=overrides_b64,
                        db_query_all=query_all,
                        group_id=group_id,
                    )
                except Exception as exc:
                    log.error(f"Failover dispatch to RunPod failed for {new_job_id}: {exc}")
        elif best_machine.get("machine_type") == "modal_serverless" and modal_dispatch.is_enabled():
            group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
            if group:
                blend_url = (
                    f"{modal_dispatch.PUBLIC_BACKEND_URL}"
                    f"/render-groups/{group_id}/input/{group['input_filename']}"
                )
                overrides_b64 = base64.b64encode(
                    (task.get("render_overrides_json") or "{}").encode()
                ).decode()
                try:
                    modal_job_id = modal_dispatch.dispatch_and_save(
                        job_id=new_job_id,
                        blend_url=blend_url,
                        frame_start=new_start,
                        frame_end=new_end,
                        frame_step=step,
                        render_overrides_b64=overrides_b64,
                        machine_id=best_machine["id"],
                        db_execute=execute,
                    )
                    modal_dispatch.start_monitoring_thread(
                        job_id=new_job_id,
                        provider_job_id=modal_job_id,
                        db_execute=execute,
                        db_query_one=query_one,
                        now_iso=now_iso,
                        machine_id=best_machine["id"],
                        blend_url=blend_url,
                        render_overrides_b64=overrides_b64,
                        db_query_all=query_all,
                        group_id=group_id,
                    )
                except Exception as exc:
                    log.error(f"Failover dispatch to Modal failed for {new_job_id}: {exc}")

    return new_job_ids


def choose_retry_machine(group_id: str, failed_machine_id: str) -> str | None:
    """Pick the best available machine from the current available pool.

    Prefers: serverless (instant) > other available machines.
    Avoids the machine that just failed.
    """
    rows = query_all(
        """
        SELECT * FROM machines
        WHERE status = 'available' AND id != %s
        ORDER BY gpu_vram_gb DESC
        """,
        (failed_machine_id,),
    )
    if not rows:
        # No other machines available â€” fall back to the same machine
        return failed_machine_id

    # Prefer serverless endpoints (always available, instant spin-up)
    serverless = [r for r in rows if _is_serverless(r.get("machine_type", ""))]
    if serverless:
        return serverless[0]["id"]
    return rows[0]["id"]


def _machine_type_of(machine_id: str) -> str:
    row = query_one("SELECT machine_type FROM machines WHERE id = %s", (machine_id,))
    return row["machine_type"] if row else "windows"


class CreateRenderGroupPayload(BaseModel):
    machine_ids: list[str] | None = None
    filename: str | None = None
    file_size_bytes: int | None = None
    source_asset_id: str | None = None


@app.post("/render-groups/create")
def create_render_group(payload: CreateRenderGroupPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Create a render group for distributed rendering across multiple machines."""
    # Explicit machine mode keeps legacy behavior.
    if payload.machine_ids:
        for mid in payload.machine_ids:
            machine = query_one(
                "SELECT id FROM machines WHERE id = %s AND status = 'available'",
                (mid,),
            )
            if not machine:
                raise HTTPException(
                    status_code=400,
                    detail=f"Machine {mid[:8]}... is not available",
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
        input_filename = sanitize_filename(source_asset["input_filename"])
        validate_job_input_filename(input_filename)
        r2_key = source_asset["r2_key"]
        if not r2_key:
            raise HTTPException(status_code=400, detail="Saved input file has no storage key")
        if not storage.file_exists(r2_key):
            raise HTTPException(status_code=400, detail="Saved input file is missing from storage")
        source_asset_id = source_asset["id"]
        status = "pending"
    else:
        if not payload.filename:
            raise HTTPException(status_code=400, detail="filename is required when source_asset_id is not provided")
        input_filename = sanitize_filename(payload.filename)
        validate_job_input_filename(input_filename)
        if payload.file_size_bytes is not None:
            file_size_bytes = validate_upload_size(payload.file_size_bytes)
            multipart_required = file_size_bytes > SINGLE_PUT_MAX_BYTES
        r2_key = f"jobs/{group_id}/input/{input_filename}"
        upload_url = storage.generate_presigned_upload_url(r2_key)
        status = "uploading"

    execute(
        """
        INSERT INTO render_groups (id, input_filename, r2_input_key, total_frames,
                                    frame_start, frame_end, frame_step, status, submitted_at, user_id, source_asset_id)
        VALUES (%s, %s, %s, 0, 1, 1, 1, %s, %s, %s, %s)
        """,
        (group_id, input_filename, r2_key, status, now_iso(), current_user["uid"], source_asset_id),
    )

    if source_asset:
        execute(
            "UPDATE user_input_files SET last_used_at = %s, updated_at = %s WHERE id = %s AND user_id = %s",
            (now_iso(), now_iso(), source_asset["id"], current_user["uid"]),
        )

    source_asset_payload = serialize_input_file_asset(source_asset) if source_asset else None

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


@app.post("/render-groups/{group_id}/multipart-upload/init")
def init_render_group_multipart_upload(
    group_id: str,
    payload: MultipartInitPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    group = query_one(
        "SELECT id, r2_input_key, status, user_id FROM render_groups WHERE id = %s",
        (group_id,),
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    ensure_render_group_owner(group, current_user)
    if group["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Render group is not in uploading state")

    file_size_bytes = validate_upload_size(payload.file_size_bytes)
    part_size_bytes = choose_multipart_part_size(file_size_bytes, payload.part_size_bytes)
    total_parts = compute_total_parts(file_size_bytes, part_size_bytes)
    r2_key = group["r2_input_key"]

    try:
        upload_id = storage.create_multipart_upload(
            r2_key,
            content_type=(payload.content_type or "application/octet-stream"),
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


@app.post("/render-groups/{group_id}/multipart-upload/part-urls")
def render_group_multipart_part_urls(
    group_id: str,
    payload: MultipartPartUrlsPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    group = query_one(
        "SELECT id, r2_input_key, status, user_id FROM render_groups WHERE id = %s",
        (group_id,),
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    ensure_render_group_owner(group, current_user)
    if group["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Render group is not in uploading state")

    r2_key = group["r2_input_key"]
    numbers = normalize_part_numbers(payload.part_numbers)
    urls: dict[str, str] = {}
    for n in numbers:
        urls[str(n)] = storage.generate_presigned_upload_part_url(
            r2_key,
            payload.upload_id,
            n,
            expires_in=3600,
        )
    return {"upload_id": payload.upload_id, "urls": urls}


@app.post("/render-groups/{group_id}/multipart-upload/complete")
def complete_render_group_multipart_upload(
    group_id: str,
    payload: MultipartCompletePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    group = query_one(
        "SELECT id, r2_input_key, status, user_id FROM render_groups WHERE id = %s",
        (group_id,),
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    ensure_render_group_owner(group, current_user)
    if group["status"] != "uploading":
        raise HTTPException(status_code=409, detail="Render group is not in uploading state")

    r2_key = group["r2_input_key"]
    parts = normalize_completed_parts(payload.parts)
    try:
        result = storage.complete_multipart_upload(r2_key, payload.upload_id, parts)
    except Exception as exc:
        log.error("Failed to complete multipart upload for render group %s: %s", group_id, exc)
        raise HTTPException(status_code=400, detail=f"Failed to complete multipart upload: {exc}")

    return {
        "success": True,
        "upload_id": payload.upload_id,
        "etag": result.get("ETag"),
        "location": result.get("Location"),
        "key": result.get("Key"),
    }


@app.post("/render-groups/{group_id}/multipart-upload/abort")
def abort_render_group_multipart_upload(
    group_id: str,
    payload: MultipartAbortPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    group = query_one(
        "SELECT id, r2_input_key, user_id FROM render_groups WHERE id = %s",
        (group_id,),
    )
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    ensure_render_group_owner(group, current_user)

    r2_key = group["r2_input_key"]
    try:
        storage.abort_multipart_upload(r2_key, payload.upload_id)
    except Exception as exc:
        log.warning("Failed to abort multipart upload for render group %s: %s", group_id, exc)
        raise HTTPException(status_code=400, detail=f"Failed to abort multipart upload: {exc}")

    return {"success": True, "upload_id": payload.upload_id}


class ConfirmRenderGroupPayload(BaseModel):
    machine_ids: list[str] | None = None
    frame_start: int | None = None
    frame_end: int | None = None
    frame_step: int | None = None
    render_overrides: dict[str, Any] | None = None
    scheduling: dict[str, Any] | None = None
    analysis_snapshot: dict[str, Any] | None = None


@app.post("/render-groups/{group_id}/confirm-upload")
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
    ensure_render_group_owner(group, current_user)
    if group["status"] not in ("uploading", "pending"):
        raise HTTPException(status_code=400, detail="Render group not in uploading state")

    r2_key = group["r2_input_key"]
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=400, detail="File not found in storage")

    render_overrides = normalize_render_overrides(payload.render_overrides)
    scheduling = normalize_scheduling(payload.scheduling)
    analysis_snapshot = payload.analysis_snapshot if isinstance(payload.analysis_snapshot, dict) else {}
    analysis_warnings = extract_analysis_warnings(analysis_snapshot)

    timeline = render_overrides.get("timeline", {})

    # Backward compatibility precedence:
    # explicit payload frame range > render_overrides.timeline > parsed upload
    if payload.frame_start is not None and payload.frame_end is not None:
        frame_start = int(payload.frame_start)
        frame_end = int(payload.frame_end)
        frame_step = int(payload.frame_step or 1)
    elif timeline.get("frame_start") is not None and timeline.get("frame_end") is not None:
        frame_start = int(timeline.get("frame_start"))
        frame_end = int(timeline.get("frame_end"))
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
    total_frames = ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0

    if total_frames <= 0:
        raise HTTPException(status_code=400, detail="No renderable frames found in .blend file")

    execute(
        """
        UPDATE render_groups
        SET total_frames = %s,
            frame_start = %s,
            frame_end = %s,
            frame_step = %s,
            render_overrides_json = %s,
            scheduling_json = %s,
            analysis_snapshot_json = %s,
            analysis_warnings_json = %s,
            status = 'pending'
        WHERE id = %s
        """,
        (
            total_frames,
            frame_start,
            frame_end,
            frame_step,
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
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            analysis_snapshot=analysis_snapshot,
            render_overrides=render_overrides,
            scheduling=scheduling,
            used_at=group.get("submitted_at") or now_iso(),
        )

    machines: list[dict[str, Any]] = []
    if payload.machine_ids:
        for mid in payload.machine_ids:
            m = query_one("SELECT * FROM machines WHERE id = %s", (mid,))
            if not m:
                raise HTTPException(status_code=400, detail=f"Machine {mid[:8]}... not found")
            machines.append(m)
    else:
        machines = query_available_machines()
        if not machines:
            raise HTTPException(status_code=400, detail="No available machines right now. Try again shortly.")

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
        for i, assignment in enumerate(assignments):
            assignment["chunk_index"] = i
            assignment["chunk_size_frames"] = None

    # Expand serverless farms into multiple parallel workers
    assignments = expand_serverless_assignments(assignments)
    for i, a in enumerate(assignments):
        a["chunk_index"] = i
    try:
        assignment_counts: dict[str, int] = {}
        for a in assignments:
            mt = a.get("machine_type", "unknown")
            assignment_counts[mt] = assignment_counts.get(mt, 0) + 1
        log.info(
            f"Render group {group_id}: assignment counts by machine_type = "
            f"{assignment_counts}"
        )
    except Exception:
        pass

    tasks = []
    max_retries = scheduling.get("max_retries_per_chunk", 0)
    priority = scheduling.get("priority", 0)
    overrides_json = json.dumps(render_overrides)
    for a in assignments:
        job_id = str(uuid4())
        execute(
            """
            INSERT INTO jobs (id, machine_id, group_id, input_filename, status,
                              total_frames, rendered_frames, output_files,
                              frame_start, frame_end, frame_step,
                              render_overrides_json, attempt, max_retries, priority,
                              chunk_index, chunk_size_frames, submitted_at)
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                job_id,
                a["machine_id"],
                group_id,
                group["input_filename"],
                a["total_frames"],
                a["frame_start"],
                a["frame_end"],
                a["frame_step"],
                overrides_json,
                0,
                max_retries,
                priority,
                a.get("chunk_index"),
                a.get("chunk_size_frames"),
                now_iso(),
            ),
        )
        # Serverless machines are always-available; don't flip them to 'processing'
        if not _is_serverless(a.get("machine_type", "")):
            execute(
                "UPDATE machines SET status = 'processing' WHERE id = %s",
                (a["machine_id"],),
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

    # Dispatch runpod_serverless tasks immediately
    if runpod_dispatch.is_enabled():
        blend_url = (
            f"{runpod_dispatch.PUBLIC_BACKEND_URL}"
            f"/render-groups/{group_id}/input/{group['input_filename']}"
        )
        overrides_b64 = base64.b64encode(overrides_json.encode()).decode()
        for i, task in enumerate(tasks):
            if task.get("machine_id") and _machine_type_of(task["machine_id"]) == "runpod_serverless":
                if i > 0:
                    time.sleep(0.15)
                try:
                    rp_job_id = runpod_dispatch.dispatch_and_save(
                        job_id=task["job_id"],
                        blend_url=blend_url,
                        frame_start=task["frame_start"],
                        frame_end=task["frame_end"],
                        frame_step=task["frame_step"],
                        render_overrides_b64=overrides_b64,
                        machine_id=task["machine_id"],
                        db_execute=execute,
                    )
                    runpod_dispatch.start_polling_thread(
                        job_id=task["job_id"],
                        runpod_job_id=rp_job_id,
                        db_execute=execute,
                        db_query_one=query_one,
                        now_iso=now_iso,
                        machine_id=task["machine_id"],
                        blend_url=blend_url,
                        render_overrides_b64=overrides_b64,
                        db_query_all=query_all,
                        group_id=group_id,
                    )
                except Exception as exc:
                    log.error(f"Failed to dispatch job {task['job_id']} to RunPod: {exc}")

    # Dispatch modal_serverless tasks in background so API response is not
    # blocked by long Modal web endpoint requests.
    if modal_dispatch.is_enabled():
        blend_url = (
            f"{modal_dispatch.PUBLIC_BACKEND_URL}"
            f"/render-groups/{group_id}/input/{group['input_filename']}"
        )
        overrides_b64 = base64.b64encode(overrides_json.encode()).decode()
        modal_tasks = [
            t for t in tasks
            if t.get("machine_id") and _machine_type_of(t["machine_id"]) == "modal_serverless"
        ]

        def _dispatch_modal_task(task: dict):
            try:
                modal_job_id = modal_dispatch.dispatch_and_save(
                    job_id=task["job_id"],
                    blend_url=blend_url,
                    frame_start=task["frame_start"],
                    frame_end=task["frame_end"],
                    frame_step=task["frame_step"],
                    render_overrides_b64=overrides_b64,
                    machine_id=task["machine_id"],
                    db_execute=execute,
                )
                modal_dispatch.start_monitoring_thread(
                    job_id=task["job_id"],
                    provider_job_id=modal_job_id,
                    db_execute=execute,
                    db_query_one=query_one,
                    now_iso=now_iso,
                    machine_id=task["machine_id"],
                    blend_url=blend_url,
                    render_overrides_b64=overrides_b64,
                    db_query_all=query_all,
                    group_id=group_id,
                )
            except Exception as exc:
                log.error(f"Failed to dispatch job {task['job_id']} to Modal: {exc}")

        for i, task in enumerate(modal_tasks):
            if i > 0:
                time.sleep(0.05)
            threading.Thread(
                target=_dispatch_modal_task,
                args=(task,),
                daemon=True,
                name=f"modal-dispatch-{task['job_id'][:8]}",
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


@app.post("/render-groups/cancel-all")
def cancel_all_render_groups() -> dict[str, Any]:
    """Cancel ALL active render groups and their jobs. No auth required (admin utility)."""
    groups = query_all(
        "SELECT id FROM render_groups WHERE status IN ('pending', 'running', 'uploading')",
    )
    total_cancelled = 0
    for g in groups:
        result = cancel_render_group(g["id"])
        total_cancelled += result.get("cancelled_jobs", 0)
    return {"success": True, "cancelled_groups": len(groups), "cancelled_jobs": total_cancelled}


@app.post("/render-groups/{group_id}/cancel")
def cancel_render_group(group_id: str) -> dict[str, Any]:
    """Cancel all pending/running jobs in a render group and stop RunPod workers."""
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")

    jobs = query_all(
        "SELECT * FROM jobs WHERE group_id = %s AND status IN ('pending', 'running')",
        (group_id,),
    )

    cancelled_count = 0
    for job in jobs:
        # Cancel RunPod job if it has one
        rp_job_id = job.get("runpod_job_id")
        job_machine_type = _machine_type_of(job["machine_id"])
        if rp_job_id and job_machine_type == "runpod_serverless" and runpod_dispatch.is_enabled():
            try:
                runpod_dispatch.cancel_job(rp_job_id, job["machine_id"])
            except Exception as exc:
                log.warning(f"Failed to cancel RunPod job {rp_job_id}: {exc}")
        elif rp_job_id and job_machine_type == "modal_serverless" and modal_dispatch.is_enabled():
            try:
                modal_dispatch.cancel_job(rp_job_id, job["machine_id"])
            except Exception as exc:
                log.warning(f"Failed to cancel Modal job {rp_job_id}: {exc}")

        execute(
            "UPDATE jobs SET status = 'cancelled', completed_at = %s, error = 'Cancelled by user' WHERE id = %s",
            (now_iso(), job["id"]),
        )
        # Release non-serverless machines
        if not _is_serverless(job_machine_type):
            execute(
                "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
                (now_iso(), job["machine_id"]),
            )
        cancelled_count += 1

    execute(
        "UPDATE render_groups SET status = 'cancelled', completed_at = %s WHERE id = %s",
        (now_iso(), group_id),
    )

    return {"success": True, "cancelled_jobs": cancelled_count}


@app.get("/render-groups")
def list_render_groups(current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    groups = query_all(
        "SELECT id FROM render_groups WHERE user_id = %s ORDER BY submitted_at DESC",
        (current_user["uid"],),
    )
    with request_conn():
        return [_get_render_group_inner(g["id"]) for g in groups]


@app.get("/render-groups/{group_id}")
def get_render_group(group_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Get render group status with per-task progress."""
    group = query_one("SELECT user_id FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group.get("user_id") and group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    with request_conn():
        return _get_render_group_inner(group_id)


def _get_render_group_inner(group_id: str) -> dict[str, Any]:
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    resolved_render_settings = normalize_render_overrides(
        parse_json_object(group.get("render_overrides_json"), {})
    )
    scheduling = normalize_scheduling(parse_json_object(group.get("scheduling_json"), {}))
    analysis_warnings = parse_json_list(group.get("analysis_warnings_json"), [])

    jobs = query_all(
        "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC",
        (group_id,),
    )

    # Check for failover (stale machines with running tasks)
    if group["status"] in ("pending", "running"):
        _check_failover(group_id, jobs)
        # Re-fetch jobs after potential reassignment
        jobs = query_all(
            "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC",
            (group_id,),
        )

    # Build tasks with machine info
    tasks = []
    for job in jobs:
        machine = query_one("SELECT * FROM machines WHERE id = %s", (job["machine_id"],))
        tasks.append(serialize_render_group_task(job, machine))

    # Compute overall status
    statuses = [j["status"] for j in jobs]
    total_rendered = sum(t["rendered_frames"] or 0 for t in tasks)
    total_frames = group["total_frames"] or 0
    no_active = not any(s in ("pending", "running") for s in statuses)

    if (all(s == "done" for s in statuses) or
            (no_active and any(s == "done" for s in statuses) and total_frames > 0 and total_rendered >= total_frames)):
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
                "UPDATE render_groups SET status = 'running' WHERE id = %s",
                (group_id,),
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

    # Aggregate progress
    overall_pct = None
    if total_frames > 0:
        overall_pct = round(min(100.0, total_rendered / total_frames * 100), 1)
    if overall_status == "done":
        overall_pct = 100.0
    available_output_count = sum(t.get("output_files_count") or 0 for t in tasks)
    latest_candidates = [t.get("latest_output_file") for t in tasks if t.get("latest_output_file")]
    latest_output = latest_output_filename(latest_candidates) if latest_candidates else None

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
        "analysis_warnings": analysis_warnings,
        "overall_rendered_frames": total_rendered,
        "overall_progress_pct": overall_pct,
        "available_output_files_count": available_output_count,
        "latest_output_file": latest_output,
        "tasks": tasks,
    }


def build_render_group_output_entries(group_id: str) -> list[dict[str, Any]]:
    jobs = query_all(
        "SELECT id, status, output_files, frame_start, submitted_at FROM jobs WHERE group_id = %s ORDER BY submitted_at ASC, frame_start ASC",
        (group_id,),
    )
    dedup_by_filename: dict[str, dict[str, Any]] = {}
    for job in jobs:
        for item in build_job_output_entries(job):
            # If retries produced the same frame filename more than once,
            # keep the most recently submitted job's copy.
            dedup_by_filename[item["filename"]] = item
    entries = list(dedup_by_filename.values())
    entries.sort(key=lambda item: output_frame_sort_key(item.get("filename", "")))
    return entries


@app.get("/render-groups/{group_id}/outputs")
def list_render_group_outputs(group_id: str, current_user: dict = Depends(get_current_user)):
    """
    List all currently available output files in a render group.
    This endpoint is safe to call while rendering is still in progress.
    """
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group.get("user_id") and group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")
    files = build_render_group_output_entries(group_id)
    return {
        "group_id": group_id,
        "status": group.get("status"),
        "files": files,
        "count": len(files),
    }


@app.get("/render-groups/{group_id}/download")
def download_render_group_output(group_id: str, current_user: dict = Depends(get_current_user)):
    """Return all output files once no tasks are pending/running."""
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group.get("user_id") and group["user_id"] != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Access denied")

    all_jobs = query_all("SELECT status FROM jobs WHERE group_id = %s", (group_id,))
    pending_or_running = [j for j in all_jobs if j["status"] in ("pending", "running")]
    if pending_or_running:
        raise HTTPException(status_code=400, detail="Render group has tasks still in progress")

    files = build_render_group_output_entries(group_id)
    if not files:
        raise HTTPException(status_code=404, detail="No output files found")

    return {"files": files, "group_id": group_id}


# -----------------------------------------------
# Docker image distribution
# -----------------------------------------------
@app.get("/docker/image/version")
def get_docker_image_version() -> dict[str, str]:
    try:
        sha_data = storage.download_file("docker/pcrent-render.sha256")
        sha = sha_data.decode().strip().split()[0]
        return {"version": "v1.0.0", "sha256": sha}
    except Exception:
        raise HTTPException(status_code=404, detail="No image available")


@app.get("/docker/image")
def download_docker_image():
    if not storage.file_exists("docker/pcrent-render.tar.gz"):
        raise HTTPException(status_code=404, detail="Image not found")
    url = storage.generate_presigned_url("docker/pcrent-render.tar.gz", expires_in=7200)
    return RedirectResponse(url=url)


@app.get("/docker/linux-image/version")
def get_linux_docker_image_version() -> dict[str, str]:
    try:
        sha_data = storage.download_file("docker/linux/pcrent-render-linux.sha256")
        sha = sha_data.decode().strip().split()[0]
        return {"version": "v1.0.0", "sha256": sha}
    except Exception:
        raise HTTPException(status_code=404, detail="No Linux image available")


@app.get("/docker/linux-image")
def download_linux_docker_image():
    key = "docker/linux/pcrent-render-linux.tar.gz"
    if not storage.file_exists(key):
        raise HTTPException(status_code=404, detail="Linux image not found")
    url = storage.generate_presigned_url(key, expires_in=7200)
    return RedirectResponse(url=url)


# -----------------------------------------------
# Desktop installer distribution
# -----------------------------------------------
@app.get("/releases/latest")
def download_latest_release():
    if not storage.file_exists("releases/PCRentAgent-Setup.exe"):
        raise HTTPException(status_code=404, detail="No release available")
    url = storage.generate_presigned_url("releases/PCRentAgent-Setup.exe", expires_in=7200)
    return RedirectResponse(url=url)


