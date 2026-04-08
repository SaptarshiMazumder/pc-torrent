import base64
import json
import os
import re
import subprocess
import sys

import bpy

PROGRESS_PREFIX = "PCR_PROGRESS "


def log(msg: str) -> None:
    print(msg, flush=True)


def _coerce_int(value, minimum=None, maximum=None):
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


def _coerce_float(value, minimum=None, maximum=None):
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


def _coerce_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _read_overrides():
    raw = os.environ.get("RENDER_OVERRIDES_JSON", "").strip()
    raw_b64 = os.environ.get("RENDER_OVERRIDES_B64", "").strip()

    if not raw and raw_b64:
        try:
            raw = base64.b64decode(raw_b64).decode("utf-8", errors="strict")
        except Exception as exc:
            log(f"[RENDER_DRIVER] Failed to decode RENDER_OVERRIDES_B64: {exc}")

    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"[RENDER_DRIVER] Failed to parse overrides JSON: {exc}")
        return {}

    if not isinstance(parsed, dict):
        log("[RENDER_DRIVER] Overrides payload is not an object; ignoring.")
        return {}

    return parsed


def _set_attr_safe(target, attr, value):
    if value is None:
        return
    if not hasattr(target, attr):
        return
    try:
        setattr(target, attr, value)
    except Exception as exc:
        log(f"[RENDER_DRIVER] Could not set {attr}={value!r}: {exc}")


def _scene_from_overrides(overrides: dict):
    scene_name = overrides.get("scene_name")
    if isinstance(scene_name, str) and scene_name:
        scene = bpy.data.scenes.get(scene_name)
        if scene:
            return scene
        log(f"[RENDER_DRIVER] Requested scene '{scene_name}' was not found. Using current scene.")
    return bpy.context.scene


def _read_env_timeline():
    return {
        "frame_start": _coerce_int(os.environ.get("FRAME_START"), minimum=1),
        "frame_end": _coerce_int(os.environ.get("FRAME_END"), minimum=1),
        "frame_step": _coerce_int(os.environ.get("FRAME_STEP"), minimum=1),
    }


def _apply_timeline(scene, overrides: dict):
    timeline = overrides.get("timeline") if isinstance(overrides.get("timeline"), dict) else {}
    env_timeline = _read_env_timeline()

    frame_start = env_timeline["frame_start"]
    if frame_start is None:
        frame_start = _coerce_int(timeline.get("frame_start"), minimum=1) or int(scene.frame_start)

    frame_end = env_timeline["frame_end"]
    if frame_end is None:
        frame_end = _coerce_int(timeline.get("frame_end"), minimum=1) or int(scene.frame_end)

    frame_step = env_timeline["frame_step"]
    if frame_step is None:
        frame_step = _coerce_int(timeline.get("frame_step"), minimum=1) or int(scene.frame_step or 1)

    if frame_end < frame_start:
        raise RuntimeError(f"Invalid frame range: start={frame_start}, end={frame_end}")

    scene.frame_start = int(frame_start)
    scene.frame_end = int(frame_end)
    scene.frame_step = int(max(1, frame_step))

    fps = _coerce_float(timeline.get("fps"), minimum=1.0)
    if fps is not None:
        scene.render.fps = int(round(fps))

    frame_map_old = _coerce_int(timeline.get("frame_map_old"), minimum=1)
    frame_map_new = _coerce_int(timeline.get("frame_map_new"), minimum=1)
    if frame_map_old is not None:
        scene.render.frame_map_old = frame_map_old
    if frame_map_new is not None:
        scene.render.frame_map_new = frame_map_new


def _apply_output(scene, overrides: dict):
    output = overrides.get("output") if isinstance(overrides.get("output"), dict) else {}
    image_settings = scene.render.image_settings

    output_dir = os.environ.get("OUTPUT_DIR", "/output")
    default_path = f"{output_dir.rstrip('/')}/frame####"
    path_pattern = output.get("path_pattern")
    if not isinstance(path_pattern, str) or not path_pattern.strip():
        path_pattern = default_path
    scene.render.filepath = path_pattern

    file_format = output.get("file_format")
    if isinstance(file_format, str) and file_format:
        _set_attr_safe(image_settings, "file_format", file_format)
    else:
        # Default to PNG for distributed rendering — video formats cannot be split across workers
        _set_attr_safe(image_settings, "file_format", "PNG")

    color_mode = output.get("color_mode")
    if isinstance(color_mode, str) and color_mode:
        _set_attr_safe(image_settings, "color_mode", color_mode)

    color_depth = output.get("color_depth")
    if isinstance(color_depth, str) and color_depth:
        _set_attr_safe(image_settings, "color_depth", color_depth)

    compression = _coerce_int(output.get("compression"), minimum=0, maximum=100)
    if compression is not None:
        _set_attr_safe(image_settings, "compression", compression)

    quality = _coerce_int(output.get("quality"), minimum=0, maximum=100)
    if quality is not None:
        _set_attr_safe(image_settings, "quality", quality)

    exr_codec = output.get("exr_codec")
    if isinstance(exr_codec, str) and exr_codec:
        _set_attr_safe(image_settings, "exr_codec", exr_codec)


def _activate_gpu_devices(compute_type: str) -> bool:
    """Activate GPU compute devices in Cycles preferences."""
    try:
        cycles_prefs = bpy.context.preferences.addons["cycles"].preferences
    except (KeyError, AttributeError):
        log("[RENDER_DRIVER] Cycles addon not available, cannot activate GPU.")
        return False

    try:
        cycles_prefs.compute_device_type = compute_type
    except Exception as exc:
        log(f"[RENDER_DRIVER] Failed to set compute_device_type={compute_type}: {exc}")
        return False

    # Blender API differs across versions.
    try:
        cycles_prefs.get_devices()
    except Exception:
        try:
            cycles_prefs.refresh_devices()
        except Exception:
            pass

    gpu_devices = []
    for device in getattr(cycles_prefs, "devices", []):
        try:
            if device.type != "CPU":
                device.use = True
                gpu_devices.append(device.name)
            else:
                device.use = False
        except Exception:
            continue

    if gpu_devices:
        log(f"[RENDER_DRIVER] Activated {compute_type} GPU devices: {gpu_devices}")
        return True

    log(f"[RENDER_DRIVER] No {compute_type} GPU devices found (only CPU).")
    return False


def _gpu_names() -> list[str]:
    """Best-effort GPU name probe via nvidia-smi."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _apply_render(scene, overrides: dict):
    render = overrides.get("render") if isinstance(overrides.get("render"), dict) else {}
    render_settings = scene.render

    engine = render.get("engine")
    if isinstance(engine, str) and engine:
        _set_attr_safe(render_settings, "engine", engine)

    _set_attr_safe(
        render_settings,
        "resolution_x",
        _coerce_int(render.get("resolution_x"), minimum=1),
    )
    _set_attr_safe(
        render_settings,
        "resolution_y",
        _coerce_int(render.get("resolution_y"), minimum=1),
    )
    _set_attr_safe(
        render_settings,
        "resolution_percentage",
        _coerce_int(render.get("resolution_percentage"), minimum=1, maximum=1000),
    )

    cycles = getattr(scene, "cycles", None)
    if cycles is None:
        return

    _set_attr_safe(cycles, "samples", _coerce_int(render.get("cycles_samples"), minimum=1))
    _set_attr_safe(cycles, "use_adaptive_sampling", _coerce_bool(render.get("cycles_adaptive_sampling")))
    _set_attr_safe(cycles, "use_denoising", _coerce_bool(render.get("cycles_denoise")))

    device_policy = os.environ.get("DEVICE_POLICY", "").strip().upper()
    override_policy = render.get("device_policy")
    if isinstance(override_policy, str) and override_policy.strip():
        device_policy = override_policy.strip().upper()

    if device_policy == "CPU":
        _set_attr_safe(cycles, "device", "CPU")
        return

    if device_policy in {"OPTIX", "CUDA"}:
        if not _activate_gpu_devices(device_policy):
            raise RuntimeError(f"Requested Cycles device '{device_policy}' is unavailable")
        _set_attr_safe(cycles, "device", "GPU")
        return

    # AUTO: prefer OptiX, then CUDA. Fail instead of silently using CPU.
    if device_policy in {"", "AUTO"}:
        compute_order = ("OPTIX", "CUDA")
        # A100 frequently behaves better on CUDA in this worker path.
        if any("A100" in name.upper() for name in _gpu_names()):
            log("[RENDER_DRIVER] A100 detected: preferring CUDA before OPTIX.")
            compute_order = ("CUDA", "OPTIX")
        for compute_type in compute_order:
            if _activate_gpu_devices(compute_type):
                _set_attr_safe(cycles, "device", "GPU")
                return
        raise RuntimeError("No compatible Cycles GPU device found (AUTO policy)")


def _find_camera_object(camera_name: str):
    if not camera_name:
        return None
    camera = bpy.data.objects.get(camera_name)
    if camera is None:
        return None
    if getattr(camera, "type", None) != "CAMERA":
        return None
    return camera


def _iter_scene_cameras(scene):
    for obj in getattr(scene, "objects", []):
        if getattr(obj, "type", None) == "CAMERA":
            yield obj


def _ensure_scene_camera(scene) -> bool:
    """
    Ensure scene.camera is set so Blender animation render cannot fail with
    "Cannot render, no camera" on scenes that forgot to assign an active camera.
    Returns True when a camera is available after fallback.
    """
    if scene.camera and getattr(scene.camera, "type", None) == "CAMERA":
        return True

    for marker in sorted(scene.timeline_markers, key=lambda m: int(getattr(m, "frame", 0))):
        marker_cam = getattr(marker, "camera", None)
        if marker_cam and getattr(marker_cam, "type", None) == "CAMERA":
            scene.camera = marker_cam
            log(f"[RENDER_DRIVER] Fallback scene camera from marker: {marker_cam.name}")
            return True

    for cam in _iter_scene_cameras(scene):
        scene.camera = cam
        log(f"[RENDER_DRIVER] Fallback scene camera from scene objects: {cam.name}")
        return True

    for obj in bpy.data.objects:
        if getattr(obj, "type", None) == "CAMERA":
            scene.camera = obj
            log(f"[RENDER_DRIVER] Fallback scene camera from global objects: {obj.name}")
            return True

    return False


def _apply_scene_camera(scene, overrides: dict) -> str:
    camera_mode = overrides.get("camera_mode")
    if not isinstance(camera_mode, str) or not camera_mode:
        camera_mode = "auto_markers"

    camera_name = overrides.get("camera_name")
    if not isinstance(camera_name, str):
        camera_name = ""
    camera = _find_camera_object(camera_name)

    if camera_mode == "force_camera":
        if camera is None:
            raise RuntimeError("camera_mode='force_camera' requires a valid camera_name")
        scene.camera = camera
        for marker in scene.timeline_markers:
            try:
                marker.camera = camera
            except Exception:
                pass
    elif camera_mode == "auto_markers":
        if camera is not None:
            scene.camera = camera
    elif camera_mode == "camera_ranges":
        if camera is not None:
            scene.camera = camera
    else:
        log(f"[RENDER_DRIVER] Unknown camera_mode '{camera_mode}', leaving scene camera unchanged.")
        camera_mode = "auto_markers"

    return camera_mode


def _resolve_view_layer(scene, overrides: dict):
    view_layer = overrides.get("view_layer")
    if not isinstance(view_layer, str) or not view_layer:
        return None

    for layer in scene.view_layers:
        if layer.name == view_layer:
            return view_layer

    log(f"[RENDER_DRIVER] View layer '{view_layer}' not found in scene '{scene.name}'.")
    return None


def _compute_total_frames(scene):
    start = int(scene.frame_start)
    end = int(scene.frame_end)
    step = max(1, int(scene.frame_step))
    if end < start:
        return 0
    return ((end - start) // step) + 1


def _disable_default_progress_handlers():
    for handler_list in (bpy.app.handlers.render_init, bpy.app.handlers.render_write):
        for handler in list(handler_list):
            name = getattr(handler, "__name__", "")
            if name in {"on_render_init", "on_render_write"}:
                try:
                    handler_list.remove(handler)
                except Exception:
                    pass


def _emit_progress(kind: str, current_frame: int, rendered_frames: int, total_frames: int):
    payload = {
        "kind": kind,
        "current_frame": int(current_frame),
        "rendered_frames": int(rendered_frames),
        "total_frames": int(total_frames),
    }
    print(PROGRESS_PREFIX + json.dumps(payload, sort_keys=True), flush=True)


def _normalize_camera_ranges(overrides: dict):
    raw_ranges = overrides.get("camera_ranges")
    if not isinstance(raw_ranges, list):
        return []

    normalized = []
    for idx, row in enumerate(raw_ranges):
        if not isinstance(row, dict):
            continue
        if _coerce_bool(row.get("enabled")) is False:
            continue

        camera_name = row.get("camera_name")
        if not isinstance(camera_name, str) or not camera_name.strip():
            raise RuntimeError(f"camera_ranges[{idx}] is missing camera_name")
        camera_name = camera_name.strip()

        frame_start = _coerce_int(row.get("frame_start"), minimum=1)
        frame_end = _coerce_int(row.get("frame_end"), minimum=1)
        frame_step = _coerce_int(row.get("frame_step"), minimum=1) or 1
        if frame_start is None or frame_end is None or frame_end < frame_start:
            raise RuntimeError(f"camera_ranges[{idx}] has invalid frame range")

        camera_obj = _find_camera_object(camera_name)
        if camera_obj is None:
            raise RuntimeError(f"camera_ranges[{idx}] camera '{camera_name}' was not found")

        normalized.append(
            {
                "camera_name": camera_name,
                "camera_obj": camera_obj,
                "frame_start": frame_start,
                "frame_end": frame_end,
                "frame_step": frame_step,
            }
        )

    normalized.sort(key=lambda item: (item["frame_start"], item["frame_end"]))
    return normalized


def _camera_for_frame(frame: int, camera_ranges: list[dict], fallback_camera):
    for row in camera_ranges:
        if frame < row["frame_start"] or frame > row["frame_end"]:
            continue
        if (frame - row["frame_start"]) % row["frame_step"] != 0:
            continue
        return row["camera_obj"]
    return fallback_camera


def _filepath_for_frame(base_path: str, frame: int):
    if not base_path:
        base_path = "/output/frame####"

    token_match = re.search(r"#+", base_path)
    if token_match:
        width = token_match.end() - token_match.start()
        frame_str = str(frame).zfill(width)
        return f"{base_path[:token_match.start()]}{frame_str}{base_path[token_match.end():]}"

    if base_path.endswith("/") or base_path.endswith("\\"):
        return f"{base_path}frame{frame:04d}"
    return f"{base_path}{frame:04d}"


def _save_render_result_as_png(path: str) -> bool:
    """
    Save the current Render Result as PNG by copying its pixels into a fresh
    image data-block.  This bypasses scene.render.image_settings entirely, so
    it works even when the scene output format is locked to FFMPEG.
    """
    src = bpy.data.images.get("Render Result")
    if not src or src.size[0] == 0 or src.size[1] == 0:
        log("[RENDER_DRIVER] PNG fallback: Render Result not available")
        return False
    w, h = src.size[0], src.size[1]
    tmp = None
    try:
        tmp = bpy.data.images.new("_pcr_png_tmp", w, h, float_buffer=True, alpha=True)
        tmp.pixels[:] = src.pixels[:]
        tmp.file_format = "PNG"
        tmp.filepath_raw = path
        tmp.save()
        return True
    except Exception as exc:
        log(f"[RENDER_DRIVER] PNG fallback save failed: {exc}")
        return False
    finally:
        if tmp is not None:
            try:
                bpy.data.images.remove(tmp)
            except Exception:
                pass


def _render_frames_with_png_fallback(scene, selected_layer, base_path):
    """
    Frame-by-frame animation render that saves every frame as PNG regardless
    of the scene output format.  Used when the scene is locked to FFMPEG output.
    """
    frames = list(range(int(scene.frame_start), int(scene.frame_end) + 1, max(1, int(scene.frame_step))))
    if not frames:
        raise RuntimeError("No renderable frames in timeline")

    _disable_default_progress_handlers()
    _emit_progress("meta", frames[0], 0, len(frames))

    kwargs = {
        "animation": False,
        "write_still": False,
        "use_viewport": False,
        "scene": scene.name,
    }
    if selected_layer:
        kwargs["layer"] = selected_layer

    for index, frame in enumerate(frames, start=1):
        scene.frame_set(frame)
        bpy.ops.render.render(**kwargs)
        frame_path = _filepath_for_frame(base_path, frame) + ".png"
        if not _save_render_result_as_png(frame_path):
            raise RuntimeError(f"Failed to save frame {frame} as PNG")
        _emit_progress("frame", frame, index, len(frames))


def _render_animation(scene, selected_layer):
    kwargs = {
        "animation": True,
        "scene": scene.name,
        "write_still": False,
        "use_viewport": False,
    }
    if selected_layer:
        kwargs["layer"] = selected_layer
    bpy.ops.render.render(**kwargs)


def _render_with_camera_ranges(scene, selected_layer, camera_ranges):
    if not camera_ranges:
        raise RuntimeError("camera_mode='camera_ranges' requires at least one enabled camera range")

    frames = list(range(int(scene.frame_start), int(scene.frame_end) + 1, max(1, int(scene.frame_step))))
    if not frames:
        raise RuntimeError("No renderable frames in timeline")

    fallback_camera = scene.camera
    assignments = []
    for frame in frames:
        camera_obj = _camera_for_frame(frame, camera_ranges, fallback_camera)
        if camera_obj is None:
            raise RuntimeError(f"No camera resolved for frame {frame}")
        assignments.append((frame, camera_obj))

    _disable_default_progress_handlers()
    _emit_progress("meta", assignments[0][0], 0, len(assignments))

    kwargs = {
        "write_still": True,
        "scene": scene.name,
        "use_viewport": False,
    }
    if selected_layer:
        kwargs["layer"] = selected_layer

    original_path = scene.render.filepath
    try:
        for index, (frame, camera_obj) in enumerate(assignments, start=1):
            scene.camera = camera_obj
            scene.frame_set(frame)
            scene.render.filepath = _filepath_for_frame(original_path, frame)
            bpy.ops.render.render(**kwargs)
            _emit_progress("frame", frame, index, len(assignments))
    finally:
        scene.render.filepath = original_path


def main():
    overrides = _read_overrides()
    scene = _scene_from_overrides(overrides)
    if scene is None:
        raise RuntimeError("No renderable scene found")

    camera_mode = _apply_scene_camera(scene, overrides)
    _apply_timeline(scene, overrides)
    _apply_output(scene, overrides)
    _apply_render(scene, overrides)
    selected_layer = _resolve_view_layer(scene, overrides)

    total_frames = _compute_total_frames(scene)
    log(f"[RENDER_DRIVER] Scene: {scene.name}")
    log(
        "[RENDER_DRIVER] Timeline: "
        f"{scene.frame_start}..{scene.frame_end} step={scene.frame_step} total={total_frames}"
    )
    if selected_layer:
        log(f"[RENDER_DRIVER] View layer: {selected_layer}")
    if scene.camera:
        log(f"[RENDER_DRIVER] Camera: {scene.camera.name}")
    log(f"[RENDER_DRIVER] Camera mode: {camera_mode}")
    log(f"[RENDER_DRIVER] Output: {scene.render.filepath}")
    log(f"[RENDER_DRIVER] Engine: {scene.render.engine}")

    ffmpeg_locked = scene.render.image_settings.file_format == "FFMPEG"
    if ffmpeg_locked:
        log("[RENDER_DRIVER] Output format locked to FFMPEG — switching to frame-by-frame PNG rendering")

    if camera_mode == "camera_ranges":
        camera_ranges = _normalize_camera_ranges(overrides)
        log(f"[RENDER_DRIVER] Camera ranges: {len(camera_ranges)}")
        _render_with_camera_ranges(scene, selected_layer, camera_ranges)
    elif ffmpeg_locked:
        if not _ensure_scene_camera(scene):
            raise RuntimeError(
                "No camera found in the scene. Add a camera, set one as active, "
                "or use camera_ranges with valid camera names."
            )
        _render_frames_with_png_fallback(scene, selected_layer, scene.render.filepath)
    else:
        if not _ensure_scene_camera(scene):
            raise RuntimeError(
                "No camera found in the scene. Add a camera, set one as active, "
                "or use camera_ranges with valid camera names."
            )
        _render_animation(scene, selected_layer)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[RENDER_DRIVER] ERROR: {exc}", file=sys.stderr, flush=True)
        raise
