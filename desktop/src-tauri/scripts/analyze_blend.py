"""
Forge blend file analysis script.
Runs inside Blender (headless) AFTER the prepare step has produced a
packed .blend.  Emits a single JSON line on stdout:

    PCR_ANALYSIS_JSON:{...}

This is the single source of truth for the analysis snapshot consumed
by the server-side cost estimator and the desktop UI.

Phase markers (parsed by the desktop host for progress UI):
    PHASE:<name>:start
    PHASE:<name>:end:<elapsed_seconds>
"""

import json
import sys
import time
import bpy

# See prepare_and_analyze.py for rationale: stdout is block-buffered when
# piped on Windows, even with ``print(flush=True)``.
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass


def _phase_start(name):
    print(f"PHASE:{name}:start", flush=True)


def _phase_end(name, t0):
    print(f"PHASE:{name}:end:{time.monotonic() - t0:.3f}", flush=True)


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


_PASS_ATTRS = {
    "z": "use_pass_z",
    "mist": "use_pass_mist",
    "normal": "use_pass_normal",
    "position": "use_pass_position",
    "vector": "use_pass_vector",
    "uv": "use_pass_uv",
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
    "cryptomatte_object": "use_pass_cryptomatte_object",
    "cryptomatte_material": "use_pass_cryptomatte_material",
    "cryptomatte_asset": "use_pass_cryptomatte_asset",
}


def _layer_passes(layer):
    # Report only the passes the active engine actually exposes (hasattr),
    # so the UI shows an engine-correct set.  Keys mirror the worker's
    # _PASS_ATTR map so the detect -> override round-trips 1:1.
    out = {}
    for key, attr in _PASS_ATTRS.items():
        if hasattr(layer, attr):
            try:
                out[key] = bool(getattr(layer, attr, False))
            except Exception:
                pass
    return out


def _minimal_scene_payload(scene, active_name):
    frame_start = _safe_int(getattr(scene, "frame_start", 1), 1)
    frame_end = _safe_int(getattr(scene, "frame_end", frame_start), frame_start)
    frame_step = max(1, _safe_int(getattr(scene, "frame_step", 1), 1))
    total_frames = ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0
    scene_name = getattr(scene, "name", "Scene")
    camera = getattr(scene, "camera", None)
    active_camera = getattr(camera, "name", None) if camera else None
    return {
        "name": scene_name,
        "is_active": scene_name == active_name,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frame_step": frame_step,
        "total_frames": total_frames,
        "active_camera": active_camera,
        "cameras": [active_camera] if active_camera else [],
        "view_layers": [],
        "view_layer_passes": {},
        "camera_cuts": [],
    }


def _scene_payload(scene, active_name):
    payload = _minimal_scene_payload(scene, active_name)

    cameras = list(payload["cameras"])
    camera_cuts = []

    try:
        markers = sorted(
            getattr(scene, "timeline_markers", []),
            key=lambda marker: _safe_int(getattr(marker, "frame", 0), 0),
        )
        for marker in markers:
            marker_camera = None
            try:
                marker_camera = marker.camera.name if getattr(marker, "camera", None) else None
            except Exception:
                marker_camera = None
            if marker_camera:
                cameras.append(marker_camera)
            camera_cuts.append({
                "frame": _safe_int(getattr(marker, "frame", 0), 0),
                "camera_name": marker_camera,
            })
    except Exception:
        pass

    try:
        for obj in bpy.data.objects:
            if getattr(obj, "type", "") == "CAMERA":
                cameras.append(getattr(obj, "name", "Camera"))
    except Exception:
        pass

    unique_cameras = []
    for name in cameras:
        if name and name not in unique_cameras:
            unique_cameras.append(name)

    view_layers = []
    try:
        view_layers = [
            getattr(layer, "name", "")
            for layer in getattr(scene, "view_layers", [])
            if getattr(layer, "name", "")
        ]
    except Exception:
        view_layers = []

    view_layer_passes = {}
    try:
        for layer in getattr(scene, "view_layers", []):
            name = getattr(layer, "name", "")
            if name:
                view_layer_passes[name] = _layer_passes(layer)
    except Exception:
        view_layer_passes = {}

    payload["cameras"] = unique_cameras
    payload["view_layers"] = view_layers
    payload["view_layer_passes"] = view_layer_passes
    payload["camera_cuts"] = camera_cuts
    return payload


def analyze():
    # ── Scene metadata ──────────────────────────────────────────
    t0 = time.monotonic()
    _phase_start("analyze_scene")
    active_scene = bpy.context.scene
    active_name = active_scene.name if active_scene else (
        bpy.data.scenes[0].name if bpy.data.scenes else ""
    )
    scenes = []
    for scene in bpy.data.scenes:
        try:
            scenes.append(_scene_payload(scene, active_name))
        except Exception:
            scenes.append(_minimal_scene_payload(scene, active_name))
    if not scenes:
        raise RuntimeError("No scenes found in file")

    active = None
    for scene in scenes:
        if scene.get("is_active"):
            active = scene
            break
    if active is None:
        active = scenes[0]

    v = bpy.app.version
    blender_version = int(v[0]) * 100 + int(v[1])

    payload = {
        "frame_start": active["frame_start"],
        "frame_end": active["frame_end"],
        "frame_step": active["frame_step"],
        "total_frames": active["total_frames"],
        "blender_version": blender_version,
        "active_scene": active["name"],
        "cameras": active.get("cameras", []),
        "camera_cuts": active.get("camera_cuts", []),
        "view_layers": active.get("view_layers", []),
        "view_layer_passes": active.get("view_layer_passes", {}),
        "timeline_defaults": {
            "frame_start": active["frame_start"],
            "frame_end": active["frame_end"],
            "frame_step": active["frame_step"],
        },
        "output_defaults": None,
        "render_defaults": {},
        "scenes": scenes,
        "unsupported_fields": [],
    }
    _phase_end("analyze_scene", t0)

    # ── Heaviness signals (cost estimator input) ─────────────────
    t0 = time.monotonic()
    _phase_start("analyze_heaviness")
    heaviness = {}

    # Render settings
    try:
        render = active_scene.render
        engine = getattr(render, "engine", "")
        res_x = _safe_int(getattr(render, "resolution_x", 1920), 1920)
        res_y = _safe_int(getattr(render, "resolution_y", 1080), 1080)
        res_pct = _safe_int(getattr(render, "resolution_percentage", 100), 100)
        samples = 0
        if engine == "CYCLES":
            try:
                samples = _safe_int(getattr(active_scene.cycles, "samples", 0), 0)
            except Exception:
                samples = 0
        elif engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
            try:
                samples = _safe_int(getattr(active_scene.eevee, "taa_render_samples", 0), 0)
            except Exception:
                samples = 0
        heaviness["render_engine"] = engine
        heaviness["resolution_x"] = res_x
        heaviness["resolution_y"] = res_y
        heaviness["resolution_percentage"] = res_pct
        heaviness["effective_pixels"] = int(res_x * res_y * res_pct / 100)
        heaviness["samples"] = samples
    except Exception:
        pass

    # Geometry heaviness (visible meshes in the active scene)
    visible_objects = []
    try:
        visible_objects = [obj for obj in active_scene.objects if not obj.hide_render]
        mesh_objects = [obj for obj in visible_objects if obj.type == "MESH" and obj.data]
        vert_total = 0
        for obj in mesh_objects:
            try:
                vert_total += len(obj.data.vertices)
            except Exception:
                pass
        heaviness["vertex_count_total"] = vert_total
        heaviness["object_count"] = len(visible_objects)
        heaviness["mesh_count"] = len(mesh_objects)
    except Exception:
        pass

    # Asset heaviness (materials, textures, shader complexity)
    active_materials = []
    try:
        active_materials = [m for m in bpy.data.materials if m.users > 0]
        active_images = [i for i in bpy.data.images if i.users > 0]
        tex_bytes = 0
        for img in active_images:
            try:
                w, h = img.size
                channels = img.channels or 4
                tex_bytes += int(w) * int(h) * int(channels)
            except Exception:
                pass
        shader_nodes = 0
        for m in active_materials:
            try:
                if m.use_nodes and m.node_tree:
                    shader_nodes += len(m.node_tree.nodes)
            except Exception:
                pass
        heaviness["material_count"] = len(active_materials)
        heaviness["texture_count"] = len(active_images)
        heaviness["texture_total_bytes"] = tex_bytes
        heaviness["shader_node_count_total"] = shader_nodes
    except Exception:
        pass

    # Heavy-feature flags from modifiers
    uses_subdivision = False
    uses_displacement = False
    uses_particles = False
    uses_geometry_nodes = False
    geometry_nodes_complexity = 0
    try:
        for obj in visible_objects:
            for mod in getattr(obj, "modifiers", []):
                mt = getattr(mod, "type", "")
                if mt == "SUBSURF":
                    uses_subdivision = True
                elif mt == "DISPLACE":
                    uses_displacement = True
                elif mt == "PARTICLE_SYSTEM":
                    uses_particles = True
                elif mt == "NODES":
                    uses_geometry_nodes = True
                    ng = getattr(mod, "node_group", None)
                    if ng and hasattr(ng, "nodes"):
                        try:
                            geometry_nodes_complexity += len(ng.nodes)
                        except Exception:
                            pass
            if getattr(obj, "particle_systems", None):
                try:
                    if len(obj.particle_systems) > 0:
                        uses_particles = True
                except Exception:
                    pass
    except Exception:
        pass

    # Heavy-feature flags from shader graph (volumetrics, SSS)
    VOLUME_NODE_IDNAMES = (
        "ShaderNodeVolumeScatter",
        "ShaderNodeVolumeAbsorption",
        "ShaderNodeVolumePrincipled",
    )
    uses_volumetrics = False
    uses_subsurface_scattering = False
    try:
        world = active_scene.world
        if world and getattr(world, "use_nodes", False) and world.node_tree:
            for node in world.node_tree.nodes:
                if getattr(node, "bl_idname", "") in VOLUME_NODE_IDNAMES:
                    uses_volumetrics = True
                    break
        for mat in active_materials:
            try:
                if not (mat.use_nodes and mat.node_tree):
                    continue
                for node in mat.node_tree.nodes:
                    bid = getattr(node, "bl_idname", "")
                    if bid in VOLUME_NODE_IDNAMES:
                        uses_volumetrics = True
                    elif bid == "ShaderNodeSubsurfaceScattering":
                        uses_subsurface_scattering = True
            except Exception:
                pass
            if uses_volumetrics and uses_subsurface_scattering:
                break
    except Exception:
        pass

    heaviness["uses_subdivision"] = uses_subdivision
    heaviness["uses_displacement"] = uses_displacement
    heaviness["uses_particles"] = uses_particles
    heaviness["uses_geometry_nodes"] = uses_geometry_nodes
    heaviness["geometry_nodes_complexity"] = geometry_nodes_complexity
    heaviness["uses_subsurface_scattering"] = uses_subsurface_scattering
    heaviness["uses_volumetrics"] = uses_volumetrics

    payload["heaviness"] = heaviness
    _phase_end("analyze_heaviness", t0)

    print("PCR_ANALYSIS_JSON:" + json.dumps(payload, separators=(",", ":")), flush=True)


analyze()
