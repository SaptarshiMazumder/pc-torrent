"""
Shared constants, helpers, and normalization logic — no I/O, no DB.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVERLESS_TYPES: frozenset[str] = frozenset({"modal_serverless", "vast_serverless"})

DEFAULT_DEVICE_POLICY = "AUTO"
ALLOWED_DEVICE_POLICIES = {"AUTO", "OPTIX", "CUDA", "CPU"}
ALLOWED_CAMERA_MODES = {"auto_markers", "force_camera", "camera_ranges"}

MAX_UPLOAD_BYTES = 100 * 1024 * 1024 * 1024  # 100 GB
SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 * 1024  # S3 PutObject hard limit
MULTIPART_MIN_PART_SIZE_BYTES = 5 * 1024 * 1024
MULTIPART_DEFAULT_PART_SIZE_BYTES = 64 * 1024 * 1024
MULTIPART_MAX_PARTS = 10_000

PREVIEW_MAX_EDGE_PX = 512
PREVIEW_WEBP_QUALITY = 75

MIN_FRAMES_PER_WORKER = 2
MACHINE_STALE_SECONDS = 15
FAILOVER_STALE_SECONDS = 30


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def parse_output_files(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


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


# ---------------------------------------------------------------------------
# Frame file sorting
# ---------------------------------------------------------------------------

_FRAME_INDEX_PATTERN = re.compile(r"(\d+)(?=\.[^.]+$)")


def output_frame_sort_key(filename: str) -> tuple[int, str]:
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


# ---------------------------------------------------------------------------
# Render overrides normalization
# ---------------------------------------------------------------------------

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

    return {
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


def normalize_scheduling(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    return {
        "chunk_size_frames": _coerce_int(src.get("chunk_size_frames"), minimum=1),
        "max_retries_per_chunk": (
            _coerce_int(src.get("max_retries_per_chunk"), minimum=0, maximum=10)
            if src.get("max_retries_per_chunk") is not None
            else 2
        ),
        "priority": _coerce_int(src.get("priority"), minimum=-100, maximum=100) or 0,
    }


def extract_analysis_warnings(analysis_snapshot: dict[str, Any] | None) -> list[str]:
    if not isinstance(analysis_snapshot, dict):
        return []
    unsupported = analysis_snapshot.get("unsupported_fields")
    if not isinstance(unsupported, list):
        return []
    return [item.strip() for item in unsupported if isinstance(item, str) and item.strip()]


# ---------------------------------------------------------------------------
# Progress calculation
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Machine helpers
# ---------------------------------------------------------------------------

def is_serverless(machine_type: str) -> bool:
    return machine_type in SERVERLESS_TYPES
