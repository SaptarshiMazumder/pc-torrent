"""
PC Rent blend file preparation script.
Runs inside Blender (headless) before upload.

Usage:
    blender -b {blend_file} -P prepare_blend.py

Pipeline:
   1.  Scene / camera / engine / output report
   2.  Fix FFMPEG output format -> PNG (distributed rendering needs image sequences)
   3.  Make all file paths relative (portability across machines)
   4.  Relink missing linked libraries found inside the bundle
   5.  Enable addons that embedded scripts depend on
   6.  Pack all packable assets: images, UDIM, fonts, sounds, movie clips
   7.  Detect VDB volumes (cannot be packed, must be in zip)
   8.  Detect simulation caches (cannot be packed, must be in zip)
   9.  Detect Alembic / USD cache files (cannot be packed, must be in zip)
  10.  Detect compositor & VSE external file references
  11.  Bake Python expression drivers to keyframes (farm-safe animation)
  12.  Validate camera
  13.  Save prepared file

Every step is best-effort. Failures log warnings but never block upload.

Output prefixes (parsed by the desktop app):
  PREP:       informational
  PREP_WARN:  non-fatal issue (render may have artefacts / missing data)
  PREP_ERROR: fatal issue (render will likely fail)
  PREP_DONE   final line on success
"""

import bpy
import json
import os
import platform
import re
import string
import sys
import time


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    print(f"PREP: {msg}", flush=True)


def _warn(msg: str):
    print(f"PREP_WARN: {msg}", flush=True)


def _err(msg: str):
    print(f"PREP_ERROR: {msg}", flush=True)


# ── 2.  Fix FFMPEG output format ─────────────────────────────────────────────

def _fix_output_format() -> int:
    fixed = 0
    for scene in bpy.data.scenes:
        fmt = scene.render.image_settings.file_format
        if fmt == "FFMPEG":
            try:
                scene.render.image_settings.file_format = "PNG"
                _log(f"Scene '{scene.name}': output format FFMPEG -> PNG")
                fixed += 1
            except Exception as e:
                _warn(
                    f"Scene '{scene.name}': output is FFMPEG but could not be "
                    f"changed to PNG ({e}). The render driver will attempt to "
                    "handle this at render time."
                )
    return fixed


# ── 3.  Make all paths relative ───────────────────────────────────────────────

def _make_paths_relative():
    """
    Convert all absolute file paths to relative (//...).
    This is what RenderBeamer calls "path remapping" — it ensures the .blend
    file is portable across machines with different directory structures.
    Must be done BEFORE packing so pack() resolves relative paths correctly.
    """
    try:
        bpy.ops.file.make_paths_relative()
        _log("All file paths remapped to relative")
    except Exception as e:
        _warn(f"Could not make paths relative: {e}")


# ── 3b. Recover missing external file references ─────────────────────────────
#
# When a .blend references an external asset that's not where the file
# claims it is (absolute path from a different machine, sibling textures/
# folder one level too deep, etc.), Blender flags it "missing" at load
# time and pack() then fails because there's no on-disk file to read.
#
# We try to recover by walking the user's machine with
# ``bpy.ops.file.find_missing_files`` -- it builds a basename index of
# every file under a given directory and re-points missing references
# whose basenames match.  Tiered from cheap (blend's own folder) to
# expensive (every fixed drive root) with a global time budget so we
# don't hang the analyze step on a big drive.

_RECOVER_TOTAL_BUDGET_SEC = 90.0
_DRIVE_FIXED = 3  # Windows GetDriveTypeW: DRIVE_FIXED


def _count_missing_refs() -> int:
    """Total external references whose resolved path is missing on disk.

    Mirrors the set of data blocks the pack/check steps care about.
    Skips items that are already packed (no need to recover) or have
    empty filepaths (no reference to resolve).
    """
    missing = 0

    def _is_missing(item, packed_attr: str = "packed_file") -> bool:
        fp = getattr(item, "filepath", "")
        if not fp:
            return False
        if packed_attr and getattr(item, packed_attr, None):
            return False
        try:
            return not os.path.exists(bpy.path.abspath(fp))
        except Exception:
            return False

    for img in bpy.data.images:
        if img.source in ("FILE", "SEQUENCE", "MOVIE", "TILED") and _is_missing(img):
            missing += 1
    for lib in bpy.data.libraries:
        if getattr(lib, "is_missing", False):
            missing += 1
    for font in bpy.data.fonts:
        if font.filepath in ("<builtin>", ""):
            continue
        if _is_missing(font):
            missing += 1
    for snd in bpy.data.sounds:
        if _is_missing(snd):
            missing += 1
    for clip in bpy.data.movieclips:
        if _is_missing(clip, packed_attr=""):
            missing += 1
    for vol in bpy.data.volumes:
        if _is_missing(vol, packed_attr=""):
            missing += 1
    for cf in bpy.data.cache_files:
        if _is_missing(cf, packed_attr=""):
            missing += 1
    return missing


def _fixed_drive_roots() -> list[str]:
    """Return existing fixed-disk drive roots on Windows; '/' elsewhere.

    We skip removable / CD / network drives -- walking them risks
    multi-minute hangs or random media-not-ready prompts.
    """
    if platform.system() != "Windows":
        return ["/"]

    roots: list[str] = []
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            try:
                kind = kernel32.GetDriveTypeW(root)
            except Exception:
                kind = 0
            if kind == _DRIVE_FIXED:
                roots.append(root)
    except Exception as exc:
        _warn(f"Could not enumerate fixed drives: {exc}")
    return roots


def _try_find_under(directory: str, label: str, time_left: float) -> int:
    """Run find_missing_files on ``directory`` and return resolved count.

    Returns the drop in missing-ref count after the op.  The op walks
    the tree fully regardless of how many refs are missing, so a fast
    early-exit when count drops to 0 is enforced by the caller.
    """
    if time_left <= 0 or not directory:
        return 0
    if not os.path.isdir(directory):
        return 0
    before = _count_missing_refs()
    if before == 0:
        return 0
    t_start = time.monotonic()
    try:
        bpy.ops.file.find_missing_files(directory=directory, find_all=True)
    except Exception as exc:
        _warn(f"find_missing_files({label}) failed: {exc}")
        return 0
    elapsed = time.monotonic() - t_start
    after = _count_missing_refs()
    resolved = max(0, before - after)
    _log(
        f"search {label}: resolved {resolved}/{before} missing reference(s) "
        f"in {elapsed:.1f}s"
    )
    return resolved


def _recover_missing_files(blend_dir: str) -> int:
    """Tiered hunt for missing external files anywhere on the machine.

    Tiers, cheapest to most expensive, share a single time budget:
      1. The .blend's own folder
      2. The .blend's parent folder
      3. The user's home directory
      4. Every fixed drive root (C:\\, D:\\ ...)
    Stops as soon as nothing is missing OR the budget runs out.
    """
    initial = _count_missing_refs()
    if initial == 0:
        return 0
    _log(f"Recovering {initial} missing external reference(s) -- tiered search")

    deadline = time.monotonic() + _RECOVER_TOTAL_BUDGET_SEC
    total_resolved = 0

    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    # Tier 1: the .blend's own directory.
    total_resolved += _try_find_under(blend_dir, "tier1 (blend dir)", _remaining())
    if _count_missing_refs() == 0:
        return total_resolved

    # Tier 2: parent of the .blend's directory.
    parent_dir = os.path.dirname(blend_dir.rstrip("\\/")) if blend_dir else ""
    if parent_dir and parent_dir != blend_dir:
        total_resolved += _try_find_under(parent_dir, "tier2 (parent dir)", _remaining())
        if _count_missing_refs() == 0:
            return total_resolved

    # Tier 3: user home.
    home = os.path.expanduser("~")
    if home and home != blend_dir and home != parent_dir:
        total_resolved += _try_find_under(home, "tier3 (user home)", _remaining())
        if _count_missing_refs() == 0:
            return total_resolved

    # Tier 4: every fixed drive root.  Slowest by far -- this is the
    # "anywhere on the machine" case.  Each root call competes for the
    # remaining time budget.
    skip = {blend_dir, parent_dir, home}
    for root in _fixed_drive_roots():
        if _remaining() <= 0:
            _warn(
                f"Search budget ({_RECOVER_TOTAL_BUDGET_SEC:.0f}s) exhausted; "
                f"{_count_missing_refs()} reference(s) still missing"
            )
            break
        if root in skip:
            continue
        total_resolved += _try_find_under(root, f"tier4 ({root})", _remaining())
        if _count_missing_refs() == 0:
            break

    return total_resolved


# ── 4.  Relink missing libraries ─────────────────────────────────────────────

def _relink_missing_libraries(blend_dir: str) -> tuple[int, int]:
    relinked = 0
    still_missing = 0

    # Build a map of all .blend files in the bundle
    available: dict[str, list[str]] = {}
    for root, _, files in os.walk(blend_dir):
        for fname in files:
            if fname.lower().endswith(".blend"):
                available.setdefault(fname.lower(), []).append(
                    os.path.join(root, fname)
                )

    for lib in bpy.data.libraries:
        if not lib.is_missing:
            continue

        lib_name = os.path.basename(lib.filepath).lower()
        candidates = available.get(lib_name, [])

        if not candidates:
            still_missing += 1
            _warn(f"Missing library not found in bundle: {lib.filepath}")
            continue

        # Prefer the candidate whose directory path best matches the original
        orig_dir = os.path.dirname(bpy.path.abspath(lib.filepath)).lower()
        best = min(
            candidates,
            key=lambda c: _path_distance(orig_dir, os.path.dirname(c).lower()),
        )

        _log(f"Relinking missing library '{lib.filepath}' -> '{best}'")
        lib.filepath = best
        try:
            lib.reload()
            relinked += 1
            _log(f"Relinked OK: {lib_name}")
        except Exception as e:
            still_missing += 1
            _warn(f"Relink failed for '{lib_name}': {e}")

    return relinked, still_missing


def _path_distance(a: str, b: str) -> int:
    """Simple path similarity score (lower = more similar)."""
    pa = a.replace("\\", "/").strip("/").split("/")
    pb = b.replace("\\", "/").strip("/").split("/")
    common = 0
    for x, y in zip(reversed(pa), reversed(pb)):
        if x == y:
            common += 1
        else:
            break
    return max(len(pa), len(pb)) - common


# ── 5.  Enable addons that embedded scripts depend on ─────────────────────────

# Known module-name -> addon-name mappings.
# Blender 4.2+ moved built-in addons to "bl_ext.blender_org.*"
_KNOWN_ADDON_MODULES: dict[str, list[str]] = {
    "copy_global_transform": [
        "copy_global_transform",
        "bl_ext.blender_org.copy_global_transform",
    ],
    "rigify": ["rigify"],
    "io_scene_gltf2": ["io_scene_gltf2"],
    "node_wrangler": [
        "node_wrangler",
        "bl_ext.blender_org.node_wrangler",
    ],
    "add_curve_extra_objects": ["add_curve_extra_objects"],
    "animation_animall": ["animation_animall"],
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+([\w.]+)|from\s+([\w.]+)\s+import)", re.MULTILINE
)


def _enable_required_addons() -> int:
    """
    Scan text blocks for import statements and enable matching Blender addons.
    This runs on the user's machine during prepare — the user's Blender install
    has the addons available. Enabling them here means:
      - The addon's Python module is importable for the rest of this session
      - Drivers that depend on addon functions can be baked successfully
    """
    import addon_utils

    # Collect all module names imported by auto-run text blocks
    needed: set[str] = set()
    for text in bpy.data.texts:
        if not getattr(text, "use_module", False):
            continue
        body = text.as_string()
        for m in _IMPORT_RE.finditer(body):
            mod = (m.group(1) or m.group(2)).split(".")[0]
            needed.add(mod)

    if not needed:
        return 0

    enabled = 0
    for mod_name in needed:
        # Already importable?
        try:
            __import__(mod_name)
            continue
        except ImportError:
            pass

        # Try known addon names
        candidates = _KNOWN_ADDON_MODULES.get(mod_name, [mod_name])
        for addon_name in candidates:
            try:
                addon_utils.enable(addon_name, default_set=False)
                _log(f"Enabled addon '{addon_name}' (needed by embedded scripts)")
                enabled += 1
                break
            except Exception:
                continue
        else:
            _warn(
                f"Module '{mod_name}' imported by embedded scripts but could "
                "not be enabled as an addon. Drivers depending on it will be "
                "baked to keyframes."
            )

    return enabled


# ── 6.  Pack assets ───────────────────────────────────────────────────────────

def _pack_images() -> tuple[int, list[str]]:
    packed = 0
    missing = []
    for image in bpy.data.images:
        if image.source not in ("FILE", "SEQUENCE", "MOVIE", "TILED"):
            continue
        if image.packed_file:
            continue
        try:
            image.pack()
            packed += 1
            _log(f"Packed image: {image.name} (source={image.source})")
        except Exception as e:
            missing.append(image.filepath or image.name)
            _warn(f"Cannot pack image '{image.name}' ({image.filepath}): {e}")
    return packed, missing


def _pack_fonts() -> int:
    packed = 0
    for font in bpy.data.fonts:
        if font.packed_file or font.filepath in ("<builtin>", ""):
            continue
        try:
            font.pack()
            packed += 1
            _log(f"Packed font: {font.name}")
        except Exception as e:
            _warn(f"Cannot pack font '{font.name}': {e}")
    return packed


def _pack_sounds() -> int:
    packed = 0
    for sound in bpy.data.sounds:
        if sound.packed_file:
            continue
        try:
            sound.pack()
            packed += 1
            _log(f"Packed sound: {sound.name}")
        except Exception as e:
            _warn(f"Cannot pack sound '{sound.name}': {e}")
    return packed


def _check_movieclips() -> list[str]:
    """
    Movie clips (motion tracking data) cannot be packed.
    They reference external video files that must be in the zip.
    """
    issues = []
    for clip in bpy.data.movieclips:
        fp = clip.filepath
        if not fp:
            continue
        abs_path = bpy.path.abspath(fp)
        if os.path.exists(abs_path):
            _warn(
                f"Movie clip '{clip.name}' at '{fp}' — cannot be packed into "
                ".blend. Include this file in your zip archive."
            )
        else:
            _warn(
                f"Missing movie clip '{clip.name}' at '{fp}' — file not found. "
                "Motion tracking will not work."
            )
        issues.append(fp)
    return issues


# ── 7.  VDB volumes ──────────────────────────────────────────────────────────

def _check_volumes() -> list[str]:
    issues = []
    for vol in bpy.data.volumes:
        fp = getattr(vol, "filepath", "")
        if not fp:
            continue
        abs_path = bpy.path.abspath(fp)
        if os.path.exists(abs_path):
            _warn(
                f"External VDB volume '{vol.name}' at '{fp}' — "
                "cannot be packed. Include this file in your zip archive."
            )
        else:
            _err(
                f"Missing VDB volume '{vol.name}' at '{fp}' — "
                "file not found. Render will fail."
            )
        issues.append(fp)
    return issues


# ── 8.  Simulation caches ────────────────────────────────────────────────────

def _check_simulation_caches() -> list[str]:
    found = []

    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            cache = getattr(mod, "point_cache", None)
            if cache and getattr(cache, "is_baked", False):
                cache_path = bpy.path.abspath(cache.filepath) if cache.filepath else ""
                label = f"object '{obj.name}' / modifier '{mod.name}'"
                if cache_path and os.path.exists(cache_path):
                    _warn(f"Baked cache at '{cache_path}' ({label}) — include in zip.")
                    found.append(cache_path)
                elif cache_path:
                    _err(f"Baked cache MISSING at '{cache_path}' ({label}).")
                    found.append(cache_path)
                else:
                    _warn(f"Baked cache (memory/unknown path) for {label} — include cache folder in zip if using disk cache.")

            if mod.type == "FLUID":
                try:
                    domain = getattr(mod, "domain_settings", None)
                    if domain:
                        cache_dir = bpy.path.abspath(domain.cache_directory)
                        if os.path.exists(cache_dir):
                            _warn(f"Fluid/smoke cache at '{cache_dir}' (object '{obj.name}') — include in zip.")
                            found.append(cache_dir)
                except Exception:
                    pass

        for psys in getattr(obj, "particle_systems", []):
            cache = getattr(psys, "point_cache", None)
            if cache and getattr(cache, "is_baked", False):
                cache_path = bpy.path.abspath(cache.filepath) if cache.filepath else ""
                label = f"object '{obj.name}' / particles '{psys.name}'"
                if cache_path and os.path.exists(cache_path):
                    _warn(f"Baked particle cache at '{cache_path}' ({label}) — include in zip.")
                    found.append(cache_path)
                elif not cache_path:
                    _warn(f"Baked particle cache (memory path) for {label} — include cache folder in zip if using disk cache.")

    return found


# ── 9.  Alembic / USD cache files ────────────────────────────────────────────

def _check_alembic_and_usd() -> list[str]:
    issues = []
    for cf in bpy.data.cache_files:
        fp = cf.filepath
        if not fp:
            continue
        abs_path = bpy.path.abspath(fp)
        ext = os.path.splitext(fp)[1].lower()
        kind = "Alembic" if ext == ".abc" else "USD/cache"
        if os.path.exists(abs_path):
            _warn(f"External {kind} file '{cf.name}' at '{fp}' — include in zip.")
        else:
            _err(f"Missing {kind} file '{cf.name}' at '{fp}' — render will fail.")
        issues.append(fp)
    return issues


# ── 10. Compositor & VSE external file references ────────────────────────────

def _check_compositor() -> list[str]:
    """Check compositor node trees for external file references."""
    issues = []
    for scene in bpy.data.scenes:
        if not scene.use_nodes or not scene.node_tree:
            continue
        for node in scene.node_tree.nodes:
            # Movie Clip nodes reference external video files
            if node.type == "MOVIECLIP" and node.clip:
                fp = node.clip.filepath
                if fp:
                    abs_path = bpy.path.abspath(fp)
                    if not os.path.exists(abs_path):
                        _warn(f"Compositor MovieClip node '{node.name}' references missing file: {fp}")
                        issues.append(fp)

            # Image nodes — the image should already be packed by _pack_images,
            # but warn if it's still external and missing
            if node.type == "IMAGE" and node.image:
                img = node.image
                if img.source in ("FILE", "SEQUENCE", "MOVIE", "TILED") and not img.packed_file:
                    abs_path = bpy.path.abspath(img.filepath)
                    if not os.path.exists(abs_path):
                        _warn(f"Compositor Image node '{node.name}' references unpacked missing image: {img.filepath}")
                        issues.append(img.filepath)

    return issues


def _check_vse_strips() -> list[str]:
    """Check Video Sequence Editor strips for external file references."""
    issues = []
    for scene in bpy.data.scenes:
        se = scene.sequence_editor
        if not se:
            continue
        for strip in se.sequences_all:
            fp = ""
            if strip.type == "MOVIE":
                fp = getattr(strip, "filepath", "")
            elif strip.type == "SOUND":
                fp = getattr(strip.sound, "filepath", "") if strip.sound else ""
            elif strip.type == "IMAGE":
                fp = getattr(strip, "directory", "")

            if not fp:
                continue
            abs_path = bpy.path.abspath(fp)
            if not os.path.exists(abs_path):
                _warn(f"VSE strip '{strip.name}' ({strip.type}) references missing file: {fp}")
                issues.append(fp)

    return issues


# ── 11. Bake Python expression drivers to keyframes ──────────────────────────

def _iter_all_animatable():
    """Yield every ID data block that can carry animation_data / drivers."""
    yield from bpy.data.objects
    yield from bpy.data.materials
    yield from bpy.data.worlds
    yield from bpy.data.cameras
    yield from bpy.data.lights
    yield from bpy.data.node_groups
    for p in bpy.data.particles:
        yield p
    for mesh in bpy.data.meshes:
        if mesh.shape_keys:
            yield mesh.shape_keys
    for curve in bpy.data.curves:
        if hasattr(curve, "shape_keys") and curve.shape_keys:
            yield curve.shape_keys
    for lattice in bpy.data.lattices:
        if hasattr(lattice, "shape_keys") and lattice.shape_keys:
            yield lattice.shape_keys


def _bake_scripted_drivers() -> int:
    """
    Bake all SCRIPTED (Python expression) drivers to keyframes.

    Why: Python expression drivers call functions from modules that may not
    exist on the render farm (e.g. cloudrig.py uses copy_global_transform).
    By baking the driver output to keyframes, the animation is preserved
    as pure data — no Python needed at render time.

    This runs AFTER _enable_required_addons, so any addon we managed to
    enable will be active. Drivers that still can't evaluate (missing module)
    will produce fallback values or warnings.

    Only SCRIPTED drivers are baked. Simple expression drivers
    (SUMMED_CURVES, AVERAGE, TRANSFORM_CHANNEL) don't need Python.
    """
    scene = bpy.context.scene
    start = int(scene.frame_start)
    end = int(scene.frame_end)

    # ── Collect all scripted drivers ──────────────────────────────────────
    drivers_to_bake: list[tuple] = []  # (id_data, data_path, array_index)

    for id_data in _iter_all_animatable():
        ad = getattr(id_data, "animation_data", None)
        if not ad or not ad.drivers:
            continue
        for fc in ad.drivers:
            if fc.driver.type == "SCRIPTED":
                drivers_to_bake.append((id_data, fc.data_path, fc.array_index))

    if not drivers_to_bake:
        _log("No Python expression drivers to bake")
        return 0

    _log(f"Baking {len(drivers_to_bake)} scripted driver(s) across frames {start}-{end}...")

    # ── Sample driver values at every frame ───────────────────────────────
    samples: dict[tuple, list[tuple[int, float]]] = {}

    for frame in range(start, end + 1):
        scene.frame_set(frame)
        # Force full depsgraph evaluation
        try:
            dg = bpy.context.evaluated_depsgraph_get()
            dg.update()
        except Exception:
            pass

        for id_data, data_path, array_index in drivers_to_bake:
            key = (id(id_data), data_path, array_index)
            ad = getattr(id_data, "animation_data", None)
            if not ad:
                continue
            for fc in ad.drivers:
                if fc.data_path == data_path and fc.array_index == array_index:
                    try:
                        val = fc.evaluate(frame)
                        samples.setdefault(key, []).append((frame, val))
                    except Exception:
                        pass
                    break

    # ── Remove drivers and insert baked keyframes ─────────────────────────
    baked = 0
    for id_data, data_path, array_index in drivers_to_bake:
        key = (id(id_data), data_path, array_index)
        values = samples.get(key)
        if not values:
            _warn(f"Could not sample driver {id_data.name}.{data_path}[{array_index}] — skipping")
            continue

        ad = id_data.animation_data

        try:
            id_data.driver_remove(data_path, array_index)
        except Exception as e:
            _warn(f"Could not remove driver {id_data.name}.{data_path}[{array_index}]: {e}")
            continue

        # Ensure an action exists to hold keyframes
        if not ad.action:
            ad.action = bpy.data.actions.new(name=f"{id_data.name}_baked_drivers")

        try:
            fc_new = ad.action.fcurves.new(data_path=data_path, index=array_index)
            fc_new.keyframe_points.add(len(values))
            for i, (frame, val) in enumerate(values):
                kp = fc_new.keyframe_points[i]
                kp.co = (float(frame), float(val))
                kp.interpolation = "LINEAR"
            baked += 1
        except Exception as e:
            _warn(f"Could not bake driver {id_data.name}.{data_path}[{array_index}]: {e}")

    _log(f"Baked {baked}/{len(drivers_to_bake)} scripted drivers to keyframes")
    return baked


# ── 12. Scene validation ──────────────────────────────────────────────────────

def _validate_scenes() -> list[str]:
    errors = []
    active = bpy.context.scene

    if not active.camera:
        cameras = [o for o in active.objects if o.type == "CAMERA"]
        if cameras:
            active.camera = cameras[0]
            _warn(f"No active camera — auto-assigned '{cameras[0].name}'")
        else:
            # Search all objects as fallback
            all_cameras = [o for o in bpy.data.objects if o.type == "CAMERA"]
            if all_cameras:
                active.camera = all_cameras[0]
                _warn(f"No camera in active scene — assigned '{all_cameras[0].name}' from another scene")
            else:
                errors.append(f"Scene '{active.name}' has no camera")
                _err(f"Scene '{active.name}' has no camera — render will fail")

    return errors


# ── Entry point ───────────────────────────────────────────────────────────────

def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _minimal_scene_payload(scene, active_name: str) -> dict:
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
        "camera_cuts": [],
    }


def _scene_payload(scene, active_name: str) -> dict:
    payload = _minimal_scene_payload(scene, active_name)

    cameras = list(payload["cameras"])
    camera_cuts = []

    try:
        markers = sorted(getattr(scene, "timeline_markers", []), key=lambda marker: _safe_int(getattr(marker, "frame", 0), 0))
        for marker in markers:
            marker_camera = None
            try:
                marker_camera = marker.camera.name if getattr(marker, "camera", None) else None
            except Exception:
                marker_camera = None
            if marker_camera:
                cameras.append(marker_camera)
            camera_cuts.append(
                {
                    "frame": _safe_int(getattr(marker, "frame", 0), 0),
                    "camera_name": marker_camera,
                }
            )
    except Exception:
        pass

    try:
        for obj in bpy.data.objects:
            if getattr(obj, "type", "") == "CAMERA":
                cameras.append(getattr(obj, "name", "Camera"))
    except Exception:
        pass

    deduped_cameras = []
    for camera_name in cameras:
        if camera_name and camera_name not in deduped_cameras:
            deduped_cameras.append(camera_name)

    view_layers = []
    try:
        view_layers = [getattr(layer, "name", "") for layer in getattr(scene, "view_layers", []) if getattr(layer, "name", "")]
    except Exception:
        view_layers = []

    payload["cameras"] = deduped_cameras
    payload["view_layers"] = view_layers
    payload["camera_cuts"] = camera_cuts
    return payload


def _emit_analysis_json():
    active_scene = bpy.context.scene if bpy.context and bpy.context.scene else None
    if active_scene:
        active_name = active_scene.name
    elif bpy.data.scenes:
        active_name = bpy.data.scenes[0].name
    else:
        raise RuntimeError("No scenes found for analysis")

    scenes = []
    for scene in bpy.data.scenes:
        try:
            scenes.append(_scene_payload(scene, active_name))
        except Exception:
            scenes.append(_minimal_scene_payload(scene, active_name))
    if not scenes:
        raise RuntimeError("No scenes found for analysis")

    active = None
    for scene in scenes:
        if scene.get("is_active"):
            active = scene
            break
    if active is None:
        active = scenes[0]

    version = bpy.app.version
    blender_version = int(version[0]) * 100 + int(version[1])
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

    print("PCR_ANALYSIS_JSON:" + json.dumps(payload, separators=(",", ":")), flush=True)


def prepare():
    filepath = bpy.data.filepath
    if not filepath:
        _err("No blend file loaded")
        sys.exit(1)

    blend_dir = os.path.dirname(filepath)
    _log(f"Blend file: {filepath}")
    _log(f"Blender: {bpy.app.version_string}")

    # ── 1. Scene report ──────────────────────────────────────────────────
    for scene in bpy.data.scenes:
        cam = scene.camera.name if scene.camera else "NONE"
        fmt = scene.render.image_settings.file_format
        engine = scene.render.engine
        _log(
            f"Scene '{scene.name}': engine={engine}, camera={cam}, "
            f"frames={scene.frame_start}-{scene.frame_end}, output_format={fmt}"
        )

    # ── 2. Fix FFMPEG output ─────────────────────────────────────────────
    _fix_output_format()

    # ── 3. Make paths relative ───────────────────────────────────────────
    _make_paths_relative()

    # ── 3b. Recover missing files via tiered machine-wide search ────────
    _recover_missing_files(blend_dir)

    # ── 4. Relink missing libraries ──────────────────────────────────────
    total_libs = len(list(bpy.data.libraries))
    if total_libs:
        _log(f"Found {total_libs} linked library/libraries — checking...")
        relinked, still_missing = _relink_missing_libraries(blend_dir)
        _log(f"Libraries: {relinked} relinked, {still_missing} still missing")
    else:
        _log("No linked libraries")

    # ── 5. Enable addons needed by embedded scripts ──────────────────────
    _enable_required_addons()

    # ── 6. Pack assets ───────────────────────────────────────────────────
    packed_images, missing_images = _pack_images()
    packed_fonts = _pack_fonts()
    packed_sounds = _pack_sounds()
    _log(
        f"Packed: {packed_images} images, {packed_fonts} fonts, {packed_sounds} sounds. "
        f"Unpacked images: {len(missing_images)}"
    )
    if missing_images:
        preview = ", ".join(missing_images[:5])
        extra = ", ..." if len(missing_images) > 5 else ""
        _warn(f"Missing images (will render pink/black): {preview}{extra}")

    clip_issues = _check_movieclips()
    if clip_issues:
        _warn(f"{len(clip_issues)} movie clip(s) cannot be packed — include in zip.")

    # ── 7. VDB volumes ───────────────────────────────────────────────────
    vdb_issues = _check_volumes()
    if vdb_issues:
        _warn(f"{len(vdb_issues)} external VDB/volume file(s) — include in zip.")

    # ── 8. Simulation caches ─────────────────────────────────────────────
    cache_issues = _check_simulation_caches()
    if cache_issues:
        _warn(f"{len(cache_issues)} baked simulation cache(s) — include cache folders in zip.")

    # ── 9. Alembic / USD ─────────────────────────────────────────────────
    abc_issues = _check_alembic_and_usd()
    if abc_issues:
        _warn(f"{len(abc_issues)} external Alembic/USD cache file(s) — include in zip.")

    # ── 10. Compositor & VSE ─────────────────────────────────────────────
    _check_compositor()
    _check_vse_strips()

    # ── 11. Bake scripted drivers ────────────────────────────────────────
    _bake_scripted_drivers()

    # ── 12. Validate ─────────────────────────────────────────────────────
    _validate_scenes()

    # ── 13. Save ─────────────────────────────────────────────────────────
    # ``PCR_PREP_OUTPUT_PATH`` is set by the Tauri host: where the prepared
    # (packed) .blend should land.  Bare-.blend case: a fresh path in the
    # job work dir, so the user's original on disk is left untouched.
    # Zip case: the extracted .blend inside the work dir, overwritten in
    # place.  No fallback -- if the host forgot to set it, fail loud.
    output_path = os.environ["PCR_PREP_OUTPUT_PATH"]
    bpy.ops.wm.save_as_mainfile(filepath=output_path)
    _log(f"Saved prepared blend file to {output_path}")
    try:
        _emit_analysis_json()
    except Exception as e:
        _warn(f"Could not emit analysis metadata: {e}")

    print("PREP_DONE", flush=True)


prepare()
