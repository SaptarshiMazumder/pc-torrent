"""Shared constants, helpers, and normalization logic — zero I/O, zero DB."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 100 * 1024 * 1024 * 1024
SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 * 1024
MULTIPART_MIN_PART_SIZE_BYTES = 5 * 1024 * 1024
MULTIPART_DEFAULT_PART_SIZE_BYTES = 64 * 1024 * 1024
MULTIPART_MAX_PARTS = 10_000

PREVIEW_MAX_EDGE_PX = 512
PREVIEW_WEBP_QUALITY = 75

DEFAULT_DEVICE_POLICY = "AUTO"
ALLOWED_DEVICE_POLICIES = {"AUTO", "OPTIX", "CUDA", "CPU"}
ALLOWED_CAMERA_MODES = {"auto_markers", "force_camera", "camera_ranges"}

# Render priority: user-selected ordering key for the pending / dispatch
# queues.  Larger number = higher priority.  Three levels keep the UI
# simple and the ordering decisive.  Default NORMAL when no value is
# supplied or the input is out of range.
RENDER_PRIORITY_LOW = 0
RENDER_PRIORITY_NORMAL = 1
RENDER_PRIORITY_HIGH = 2
RENDER_PRIORITY_DEFAULT = RENDER_PRIORITY_NORMAL


def clamp_render_priority(value: int) -> int:
    """Clamp an integer priority into the allowed [LOW, HIGH] range.

    Belt-and-braces alongside the Pydantic validator on the API schema:
    any code path that constructs a ``DispatchContext`` programmatically
    (tests, future internal callers) still gets a sane value.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return RENDER_PRIORITY_DEFAULT
    if v < RENDER_PRIORITY_LOW:
        return RENDER_PRIORITY_LOW
    if v > RENDER_PRIORITY_HIGH:
        return RENDER_PRIORITY_HIGH
    return v


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

# Render passes the UI and worker agree on.  Keys map to Blender
# view-layer ``use_pass_*`` attributes in the worker (most 1:1, a few
# special: emission -> use_pass_emit; volume_* + denoising_data live
# on view_layer.cycles).  Engine-specific passes (eevee's
# diffuse_light etc.) are validated here; the worker silently skips
# any pass whose attribute isn't on the active engine's view-layer.
ALLOWED_RENDER_PASSES = frozenset({
    # data
    "z", "mist", "normal", "position", "vector", "uv",
    "object_index", "material_index",
    # cycles light
    "diffuse_direct", "diffuse_indirect", "diffuse_color",
    "glossy_direct", "glossy_indirect", "glossy_color",
    "transmission_direct", "transmission_indirect", "transmission_color",
    "emission", "environment", "ambient_occlusion", "shadow",
    "shadow_catcher",
    # cycles volume + denoising (view_layer.cycles.*)
    "volume_direct", "volume_indirect", "denoising_data",
    # eevee light
    "diffuse_light", "specular_light", "specular_color",
    "volume_light", "transparent",
    # cryptomatte (engine-agnostic)
    "cryptomatte_object", "cryptomatte_material", "cryptomatte_asset",
})


def _normalize_render_passes(raw: Any) -> dict[str, Any]:
    """Whitelist the render-pass override.  ``use_file_settings`` (default
    True) tells the worker to respect whatever passes the .blend already
    enables; when False, the per-pass booleans drive the view layer.
    Unknown keys are dropped.
    """
    src = raw if isinstance(raw, dict) else {}
    use_file = _coerce_bool(src.get("use_file_settings"))
    out: dict[str, Any] = {"use_file_settings": True if use_file is None else use_file}
    for key in ALLOWED_RENDER_PASSES:
        val = _coerce_bool(src.get(key))
        if val is not None:
            out[key] = val
    return out


def normalize_render_overrides(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    timeline = src.get("timeline") if isinstance(src.get("timeline"), dict) else {}
    output = src.get("output") if isinstance(src.get("output"), dict) else {}
    render = src.get("render") if isinstance(src.get("render"), dict) else {}

    camera_mode_raw = src.get("camera_mode")
    camera_mode = camera_mode_raw if camera_mode_raw in ALLOWED_CAMERA_MODES else "auto_markers"

    device_policy_raw = render.get("device_policy")
    device_policy = device_policy_raw.strip().upper() if isinstance(device_policy_raw, str) else DEFAULT_DEVICE_POLICY
    if device_policy not in ALLOWED_DEVICE_POLICIES:
        device_policy = DEFAULT_DEVICE_POLICY

    camera_ranges: list[dict[str, Any]] = []
    camera_ranges_raw = src.get("camera_ranges")
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
            camera_ranges.append({
                "camera_name": camera_name.strip(),
                "frame_start": frame_start,
                "frame_end": frame_end,
                "frame_step": frame_step,
                "enabled": _coerce_bool(item.get("enabled")) is not False,
            })

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
            "film_transparent": _coerce_bool(output.get("film_transparent")),
        },
        "passes": _normalize_render_passes(src.get("passes")),
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



def extract_analysis_warnings(analysis_snapshot: dict[str, Any] | None) -> list[str]:
    if not isinstance(analysis_snapshot, dict):
        return []
    unsupported = analysis_snapshot.get("unsupported_fields")
    if not isinstance(unsupported, list):
        return []
    return [item.strip() for item in unsupported if isinstance(item, str) and item.strip()]


# ---------------------------------------------------------------------------
# Heaviness — Phase 2 of the tiered_allocation_plan.
#
# The desktop app's local Blender analyzer writes a ``heaviness`` sub-dict
# into ``analysis_snapshot`` when a user uploads a .blend.  Cost / time
# estimators (Phase 3+) read those fields via this helper.
#
# Snapshots from older desktop builds, web uploads, or pre-Phase-2 renders
# don't have the sub-dict — we return safe defaults so consumers can
# always dot through without None-checks.
# ---------------------------------------------------------------------------

_HEAVINESS_DEFAULTS: dict[str, Any] = {
    "render_engine": "",
    "resolution_x": 1920,
    "resolution_y": 1080,
    "resolution_percentage": 100,
    "effective_pixels": 1920 * 1080,
    "samples": 0,
    "vertex_count_total": 0,
    "object_count": 0,
    "mesh_count": 0,
    "material_count": 0,
    "texture_count": 0,
    "texture_total_bytes": 0,
    "shader_node_count_total": 0,
    "uses_subdivision": False,
    "uses_displacement": False,
    "uses_particles": False,
    "uses_geometry_nodes": False,
    "geometry_nodes_complexity": 0,
    "uses_subsurface_scattering": False,
    "uses_volumetrics": False,
    # Whether the scene has Cycles adaptive sampling enabled (or the
    # EEVEE equivalent).  When True, the time analyzer applies a
    # per-engine speedup multiplier (~0.5 for Cycles, ~0.7 for EEVEE)
    # because adaptive sampling targets noisy regions and skips clean
    # ones, cutting the effective sample count.  Defaulted to False
    # because older heaviness snapshots don't include the field; the
    # multiplier becomes a no-op (1.0) in that case.
    "uses_adaptive_sampling": False,
    # Server-side fact (render_groups.r2_input_size_bytes); injected by
    # ``parse_analysis_heaviness(snapshot, file_size_bytes=...)``.  Kept
    # in the heaviness dict so analyzers/allocators take a single
    # "scene context" object rather than two separate kwargs.
    "file_size_bytes": 0,
}


def parse_analysis_heaviness(
    analysis_snapshot: dict[str, Any] | None,
    *,
    file_size_bytes: int | None = None,
) -> dict[str, Any]:
    """Pull the ``heaviness`` sub-dict out of an analysis snapshot, filling
    in defaults for any missing field.  Always returns a complete dict —
    callers never need to None-check individual keys.

    ``file_size_bytes`` is an optional server-side fact that gets stamped
    onto the returned dict (the desktop analyzer doesn't know the .blend's
    on-disk size; the server does, via ``render_groups.r2_input_size_bytes``).
    Cost/time analyzers read it from the dict via ``heaviness["file_size_bytes"]``.

    Used by the cost / time estimators to read render-heaviness signals
    without re-implementing the defaulting logic at every call site.
    """
    out = dict(_HEAVINESS_DEFAULTS)
    if isinstance(analysis_snapshot, dict):
        raw = analysis_snapshot.get("heaviness")
        if isinstance(raw, dict):
            for key, default in _HEAVINESS_DEFAULTS.items():
                if key == "file_size_bytes":
                    continue   # server-side, never read from snapshot
                if key not in raw:
                    continue
                value = raw[key]
                if isinstance(default, bool):
                    out[key] = bool(value)
                elif isinstance(default, int):
                    try:
                        out[key] = int(value)
                    except (TypeError, ValueError):
                        pass
                elif isinstance(default, str):
                    if isinstance(value, str):
                        out[key] = value
                else:
                    out[key] = value
    if file_size_bytes is not None:
        try:
            out["file_size_bytes"] = max(0, int(file_size_bytes))
        except (TypeError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# Progress calculation
# ---------------------------------------------------------------------------

def compute_progress_pct(status: str, rendered_frames: int | None, total_frames: int | None) -> float | None:
    rendered = max(0, rendered_frames or 0)
    if total_frames and total_frames > 0:
        pct = rendered / total_frames * 100
        if status == "done":
            pct = 100.0
        return round(max(0.0, min(100.0, pct)), 1)
    if status == "done":
        return 100.0
    return None


def is_serverless(machine_type: str) -> bool:
    from serverV2.core.enums import SERVERLESS_TYPE_VALUES
    return machine_type in SERVERLESS_TYPE_VALUES


def sanitize_filename(filename: str) -> str:
    name = filename.strip().replace("\\", "/").split("/")[-1]
    return re.sub(r"[^\w.\- ]", "_", name).strip() or "input.blend"
