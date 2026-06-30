import base64
import json
import os
import re
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

    # file_format is applied via Blender's -F CLI flag in render.sh, not here:
    # the headless file_format enum rejects render-only formats
    # (OPEN_EXR_MULTILAYER, FFMPEG) when assigned from Python.

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

    film_transparent = _coerce_bool(output.get("film_transparent"))
    if film_transparent is not None:
        # Alpha / transparent background lives on scene.render, not image_settings.
        _set_attr_safe(scene.render, "film_transparent", film_transparent)


# Override pass-key -> view-layer attribute (top-level, e.g.
# ``view_layer.use_pass_z``).  Most passes live here.  Resolved with
# hasattr at apply time so engine-specific gaps (Cycles vs EEVEE)
# are silently skipped instead of raising.
_PASS_ATTR = {
    # data
    "z": "use_pass_z",
    "mist": "use_pass_mist",
    "normal": "use_pass_normal",
    "position": "use_pass_position",
    "vector": "use_pass_vector",
    "uv": "use_pass_uv",
    "object_index": "use_pass_object_index",
    "material_index": "use_pass_material_index",
    # cycles light passes
    "diffuse_direct": "use_pass_diffuse_direct",
    "diffuse_indirect": "use_pass_diffuse_indirect",
    "diffuse_color": "use_pass_diffuse_color",
    "glossy_direct": "use_pass_glossy_direct",
    "glossy_indirect": "use_pass_glossy_indirect",
    "glossy_color": "use_pass_glossy_color",
    "transmission_direct": "use_pass_transmission_direct",
    "transmission_indirect": "use_pass_transmission_indirect",
    "transmission_color": "use_pass_transmission_color",
    "emission": "use_pass_emit",
    "environment": "use_pass_environment",
    "ambient_occlusion": "use_pass_ambient_occlusion",
    "shadow": "use_pass_shadow",
    "shadow_catcher": "use_pass_shadow_catcher",
    # eevee light passes (combined-style; no-op on Cycles)
    "diffuse_light": "use_pass_diffuse_light",
    "specular_light": "use_pass_specular_light",
    "specular_color": "use_pass_specular_color",
    "volume_light": "use_pass_volume_light",
    "transparent": "use_pass_transparent",
    # cryptomatte
    "cryptomatte_object": "use_pass_cryptomatte_object",
    "cryptomatte_material": "use_pass_cryptomatte_material",
    "cryptomatte_asset": "use_pass_cryptomatte_asset",
}

# Cycles-only passes whose attribute lives on ``view_layer.cycles``.
# ``denoising_data`` is one bool that internally enables Albedo +
# Normal + Depth denoising-data passes inside the multilayer EXR.
_PASS_ATTR_CYCLES_SUB = {
    "volume_direct": "use_pass_volume_direct",
    "volume_indirect": "use_pass_volume_indirect",
    "denoising_data": "denoising_store_passes",
}


def _apply_render_passes(scene, overrides: dict, selected_layer):
    """Enable render passes on the target view layer(s) for multilayer EXR.

    ``passes.use_file_settings`` true (the default) means respect whatever
    the .blend already enables -- no-op here.  Otherwise each known flag is
    written via setattr, hasattr-guarded so passes that don't exist for the
    active engine are silently skipped instead of raising.
    """
    passes = overrides.get("passes") if isinstance(overrides.get("passes"), dict) else None
    if not passes or passes.get("use_file_settings") is not False:
        return

    if selected_layer:
        targets = [vl for vl in scene.view_layers if vl.name == selected_layer]
    else:
        targets = list(scene.view_layers)

    applied = []
    for vl in targets:
        for key, attr in _PASS_ATTR.items():
            if key not in passes or not hasattr(vl, attr):
                continue
            try:
                setattr(vl, attr, bool(passes[key]))
                if passes[key]:
                    applied.append(f"{vl.name}.{key}")
            except Exception:
                continue
        cycles_sub = getattr(vl, "cycles", None)
        if cycles_sub is not None:
            for key, attr in _PASS_ATTR_CYCLES_SUB.items():
                if key not in passes or not hasattr(cycles_sub, attr):
                    continue
                try:
                    setattr(cycles_sub, attr, bool(passes[key]))
                    if passes[key]:
                        applied.append(f"{vl.name}.{key}")
                except Exception:
                    continue
    log(f"[RENDER_DRIVER] Render passes set: {applied or 'none'}")


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
    # Force OpenImageDenoise on every fleet, always.  The OptiX denoiser
    # needs the driver's nvoptix.bin weights file, which Modal's minimal
    # driver injection doesn't ship -> "Failed to create OptiX denoiser",
    # zero output.  A distributed render also splits frames across fleets,
    # so one denoiser everywhere avoids cross-fleet flicker.  OIDN is built
    # into Blender (no driver dependency) and runs on the GPU here.
    _set_attr_safe(cycles, "denoiser", "OPENIMAGEDENOISE")
    _set_attr_safe(cycles, "denoising_use_gpu", True)

    device_policy = os.environ.get("DEVICE_POLICY", "").strip().upper()
    override_policy = render.get("device_policy")
    if isinstance(override_policy, str) and override_policy.strip():
        device_policy = override_policy.strip().upper()

    if device_policy == "CPU":
        _set_attr_safe(cycles, "device", "CPU")
        return

    if device_policy in {"OPTIX", "CUDA"}:
        if _activate_gpu_devices(device_policy):
            _set_attr_safe(cycles, "device", "GPU")
        return

    # AUTO: try CUDA then OPTIX, fall back to CPU silently (Windows workers may not have GPU)
    if device_policy in {"", "AUTO"}:
        for compute_type in ("CUDA", "OPTIX"):
            if _activate_gpu_devices(compute_type):
                _set_attr_safe(cycles, "device", "GPU")
                return
        log("[RENDER_DRIVER] No GPU found, using CPU")


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


def _sanitize_camera_name(name) -> str:
    """Filesystem/R2-safe camera token.  Restricted to the charset the
    backend's ``sanitize_filename`` preserves (``[A-Za-z0-9._-]``) so the
    uploaded filename, the registered filename, and the ``output_frames``
    dedup key are byte-identical.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name or "").strip())
    return safe or "camera"


def _camera_prefixed_base(base_path: str, camera_name) -> str:
    """Inject ``{camera}_`` in front of the filename token of the output
    path so each frame lands as ``{camera}_frame####``.  The ``frame####``
    token is preserved verbatim — the backend derives ``frame_number``
    from it.
    """
    safe = _sanitize_camera_name(camera_name)
    head, tail = os.path.split(base_path)
    tail = f"{safe}_{tail}" if tail else f"{safe}_frame####"
    return os.path.join(head, tail) if head else tail


def _marker_camera_for_frame(scene, frame: int, fallback):
    """Active timeline-marker camera at ``frame`` — the binding Blender's
    native marker-driven render would use: the latest marker at or before
    the frame that carries a camera.  Falls back to the scene camera."""
    chosen = fallback
    best_frame = None
    for marker in scene.timeline_markers:
        cam = getattr(marker, "camera", None)
        if cam is None or getattr(cam, "type", None) != "CAMERA":
            continue
        mf = int(getattr(marker, "frame", 0))
        if mf <= frame and (best_frame is None or mf > best_frame):
            best_frame = mf
            chosen = cam
    return chosen


def _build_camera_assignments(scene, overrides: dict, camera_mode: str):
    """Resolve the camera for every frame in the timeline as a list of
    ``(frame, camera_obj)``.  Frame-grouping-by-camera is always on, so the
    per-frame camera drives both which camera renders the frame and the
    output filename prefix."""
    frames = list(
        range(int(scene.frame_start), int(scene.frame_end) + 1, max(1, int(scene.frame_step)))
    )
    if not frames:
        raise RuntimeError("No renderable frames in timeline")
    fallback = scene.camera

    if camera_mode == "camera_ranges":
        ranges = _normalize_camera_ranges(overrides)
        if not ranges:
            raise RuntimeError(
                "camera_mode='camera_ranges' requires at least one enabled camera range"
            )
        resolve = lambda f: _camera_for_frame(f, ranges, fallback)
    elif camera_mode == "force_camera":
        name = overrides.get("camera_name")
        forced = _find_camera_object(name if isinstance(name, str) else "")
        resolve = lambda f: forced or fallback
    else:  # auto_markers
        resolve = lambda f: _marker_camera_for_frame(scene, f, fallback)

    assignments = []
    for f in frames:
        cam = resolve(f)
        if cam is None or getattr(cam, "type", None) != "CAMERA":
            raise RuntimeError(f"No camera resolved for frame {f}")
        assignments.append((f, cam))
    return assignments


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


def _primary_basenames(scene, assignments, base_path, ffmpeg_locked):
    """Basenames of the MAIN render outputs (``scene.render.filepath``) -- one
    per (frame, camera) in the chunk.  These are the primary files; every
    other file in OUTPUT_DIR is a File Output node pass.  Recorded so the
    uploader can tag ``is_primary`` authoritatively, even when a camera's
    name collides with the pass naming convention (e.g. ``Cam_2``)."""
    ext = ".png" if ffmpeg_locked else (getattr(scene.render, "file_extension", "") or ".png")
    names = set()
    for frame, camera_obj in assignments:
        prefixed = _camera_prefixed_base(base_path, camera_obj.name)
        names.add(os.path.basename(_filepath_for_frame(prefixed, frame)) + ext)
    return names


def _write_primary_manifest(basenames):
    """Persist primary (main-render) basenames to a hidden manifest the
    uploader reads to set ``is_primary``.  Dot-prefixed so the uploader's
    dotfilter never uploads it.  Best-effort -- on failure the backend falls
    back to its filename-naming heuristic, so the render itself is unaffected."""
    output_dir = os.environ.get("OUTPUT_DIR", "/output")
    path = os.path.join(output_dir, ".primary_outputs.json")
    try:
        with open(path, "w") as fh:
            json.dump(sorted(basenames), fh)
        log(f"[RENDER_DRIVER] Primary manifest: {len(basenames)} main-render file(s)")
    except Exception as exc:
        log(f"[RENDER_DRIVER] Failed to write primary manifest: {exc}")


def _render_grouped(scene, selected_layer, assignments, base_path, ffmpeg_locked):
    """Render every frame to a ``{camera}_frame####`` path.

    Fast path: when a single camera covers the whole chunk and the scene
    output isn't locked to a video container, Blender's native animation
    render writes the entire range in one operator call with the camera-
    prefixed output path.  Otherwise we render frame-by-frame so each
    frame can carry its own camera and prefixed filename (and, when the
    scene format is FFMPEG, its own PNG-fallback save).
    """
    if not assignments:
        raise RuntimeError("No renderable frames in timeline")

    # Record the main-render basenames BEFORE rendering so the incremental
    # uploader already has the complete manifest while it uploads frames.
    _write_primary_manifest(
        _primary_basenames(scene, assignments, base_path, ffmpeg_locked)
    )

    distinct = {cam.name for _, cam in assignments}

    if not ffmpeg_locked and len(distinct) == 1:
        cam = assignments[0][1]
        scene.camera = cam
        scene.render.filepath = _camera_prefixed_base(base_path, cam.name)
        _render_animation(scene, selected_layer)
        return

    _disable_default_progress_handlers()
    _emit_progress("meta", assignments[0][0], 0, len(assignments))

    kwargs = {
        "animation": False,
        "write_still": not ffmpeg_locked,
        "use_viewport": False,
        "scene": scene.name,
    }
    if selected_layer:
        kwargs["layer"] = selected_layer

    original_path = scene.render.filepath
    try:
        for index, (frame, camera_obj) in enumerate(assignments, start=1):
            scene.camera = camera_obj
            scene.frame_set(frame)
            prefixed = _camera_prefixed_base(base_path, camera_obj.name)
            if ffmpeg_locked:
                bpy.ops.render.render(**kwargs)
                out_path = _filepath_for_frame(prefixed, frame) + ".png"
                if not _save_render_result_as_png(out_path):
                    raise RuntimeError(f"Failed to save frame {frame} as PNG")
            else:
                scene.render.filepath = _filepath_for_frame(prefixed, frame)
                bpy.ops.render.render(**kwargs)
            _emit_progress("frame", frame, index, len(assignments))
    finally:
        scene.render.filepath = original_path


def _resolve_compositor_node_tree(scene):
    """Return the compositor node tree across Blender 4.x and 5.x.
    Blender 5.x exposes ``scene.compositing_node_group`` (the new
    canonical handle); 4.x exposes ``scene.use_nodes`` +
    ``scene.node_tree`` (deprecated in 5.x).
    """
    cng = getattr(scene, "compositing_node_group", None)
    if cng is not None:
        return cng
    if getattr(scene, "use_nodes", False):
        return getattr(scene, "node_tree", None)
    return None


def _iter_file_output_nodes(node_tree):
    """Walk a node tree depth-first, yielding every
    ``CompositorNodeOutputFile`` found at any nesting depth.  Recursing
    through NodeGroup instances catches File Output nodes inside
    sub-groups."""
    if node_tree is None:
        return
    for node in node_tree.nodes:
        bl_idname = getattr(node, "bl_idname", "")
        if bl_idname == "CompositorNodeOutputFile":
            yield node
            continue
        sub = getattr(node, "node_tree", None)
        if sub is not None and sub is not node_tree:
            yield from _iter_file_output_nodes(sub)


def _patch_compositor_file_outputs(scene) -> None:
    """Redirect every ``CompositorNodeOutputFile`` so its files land flat
    in ``OUTPUT_DIR`` with names matching the worker's ``frame####.<ext>``
    convention.  See render_scripts/render_driver.py for the long
    explanation; behaviour is identical here.
    """
    node_tree = _resolve_compositor_node_tree(scene)
    if node_tree is None:
        log("[RENDER_DRIVER] No compositor node tree -- skipping File Output redirect")
        return
    output_dir = os.environ.get("OUTPUT_DIR", "/output").rstrip("/") + "/"
    file_output_nodes = list(_iter_file_output_nodes(node_tree))
    log(f"[RENDER_DRIVER] Found {len(file_output_nodes)} File Output node(s) in compositor")
    patched = 0
    for node in file_output_nodes:
        # See render_scripts/render_driver.py: prefer ``node.label``
        # (the F2-rename target) over the internal ``node.name``.
        display_name = (node.label or "").strip() or node.name
        safe_node_name = re.sub(r"[^\w.\- ]", "_", display_name).strip() or "FileOutput"
        try:
            # Blender 5.x: directory + file_name + file_output_items[].name
            # (replaced 4.x's base_path + file_slots[].path).  See
            # render_scripts/render_driver.py for the full rationale.
            if hasattr(node, "directory"):
                node.directory = output_dir
                if hasattr(node, "file_name"):
                    try:
                        node.file_name = ""
                    except Exception:
                        pass
                items = getattr(node, "file_output_items", None)
                if items is None:
                    log(f"[RENDER_DRIVER] Node '{node.name}': no file_output_items -- skipping")
                    continue
                for idx, item in enumerate(items):
                    try:
                        item.name = f"{safe_node_name}_{idx}_frame"
                    except Exception:
                        continue
            elif hasattr(node, "base_path"):
                node.base_path = output_dir
                slots = getattr(node, "file_slots", None) or ()
                for idx, slot in enumerate(slots):
                    try:
                        slot.path = f"{safe_node_name}_{idx}_frame"
                    except Exception:
                        continue
            else:
                log(
                    f"[RENDER_DRIVER] Node '{node.name}': neither "
                    "'directory' (5.x) nor 'base_path' (4.x) -- skipping"
                )
                continue
        except Exception as exc:
            log(f"[RENDER_DRIVER] Could not redirect File Output node '{node.name}': {exc}")
            continue
        patched += 1
    if patched:
        log(
            f"[RENDER_DRIVER] Redirected {patched} File Output node(s) "
            f"to {output_dir} with ``<node>_<slot>_frame####.<ext>`` naming"
        )


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
    _apply_render_passes(scene, overrides, selected_layer)
    _patch_compositor_file_outputs(scene)

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
        log("[RENDER_DRIVER] Output format locked to FFMPEG — saving frames as PNG")

    # camera_ranges resolves its own per-frame cameras; the other modes
    # need a valid active camera as the fallback for frame resolution.
    if camera_mode != "camera_ranges" and not _ensure_scene_camera(scene):
        raise RuntimeError(
            "No camera found in the scene. Add a camera, set one as active, "
            "or use camera_ranges with valid camera names."
        )

    base_path = scene.render.filepath
    assignments = _build_camera_assignments(scene, overrides, camera_mode)
    distinct = sorted({cam.name for _, cam in assignments})
    log(
        f"[RENDER_DRIVER] Grouping by camera: {len(distinct)} camera(s) "
        f"over {len(assignments)} frames -> {distinct}"
    )
    _render_grouped(scene, selected_layer, assignments, base_path, ffmpeg_locked)

    # ── Phase-11 telemetry emission ──────────────────────────────────────
    # See render_scripts/render_driver.py for rationale.  Post-render,
    # self-introspecting, try/except wrapped -- zero risk to rendering.
    try:
        _telemetry_dir = os.path.dirname(os.path.abspath(__file__))
        if _telemetry_dir and _telemetry_dir not in sys.path:
            sys.path.insert(0, _telemetry_dir)
        from telemetry_collector import TelemetryCollector
        TelemetryCollector().emit()
    except Exception as exc:
        log(f"[RENDER_DRIVER] telemetry emit failed (non-fatal): {exc}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[RENDER_DRIVER] ERROR: {exc}", file=sys.stderr, flush=True)
        raise
