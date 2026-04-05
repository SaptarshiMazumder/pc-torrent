"""
PC Rent blend file preparation script.
Runs inside Blender (headless) before upload.

Usage:
    blender -b {blend_file} -P prepare_blend.py

What it does:
  1.  Reports scene / camera / engine / output-format info
  2.  Fixes FFMPEG output format → PNG (distributed rendering requires image sequences)
  3.  Relinks missing linked libraries found inside the bundle
  4.  Packs all packable assets: images (incl. UDIM tiles), sequences, fonts, sounds
  5.  Detects VDB volumes        — cannot be packed, warns to include in zip
  6.  Detects simulation caches  — cannot be packed, warns to include in zip
  7.  Detects Alembic / USD files— cannot be packed, warns to include in zip
  8.  Warns about embedded Python scripts / drivers that run via --enable-autoexec
  9.  Validates camera and saves the prepared blend file

Output prefixes (parsed by the desktop app):
  PREP:       informational
  PREP_WARN:  non-fatal issue (render may have artefacts / missing data)
  PREP_ERROR: fatal issue (render will likely fail)
  PREP_DONE   final line on success
"""

import bpy
import os
import sys


def _log(msg: str):
    print(f"PREP: {msg}", flush=True)


def _warn(msg: str):
    print(f"PREP_WARN: {msg}", flush=True)


def _err(msg: str):
    print(f"PREP_ERROR: {msg}", flush=True)


# ---------------------------------------------------------------------------
# 2. Fix FFMPEG output format
# ---------------------------------------------------------------------------

def _fix_output_format() -> int:
    """
    Switch any scene whose output is FFMPEG to PNG.
    FFMPEG output cannot be split across render workers — each worker would
    produce an independent video file with no way to merge them correctly.
    We fix this at prepare-time so the uploaded .blend already has PNG set.
    """
    fixed = 0
    for scene in bpy.data.scenes:
        fmt = scene.render.image_settings.file_format
        if fmt == "FFMPEG":
            try:
                scene.render.image_settings.file_format = "PNG"
                _log(f"Scene '{scene.name}': output format FFMPEG → PNG")
                fixed += 1
            except Exception as e:
                _warn(
                    f"Scene '{scene.name}': output is FFMPEG but could not be changed to PNG ({e}). "
                    "The render driver will attempt to handle this at render time."
                )
    return fixed


# ---------------------------------------------------------------------------
# 3. Relink missing libraries
# ---------------------------------------------------------------------------

def _relink_missing_libraries(blend_dir: str) -> tuple[int, int]:
    """
    Try to find missing library .blend files inside the same directory tree.
    Returns (relinked, still_missing).
    """
    relinked = 0
    still_missing = 0

    available: dict[str, str] = {}
    for root, _, files in os.walk(blend_dir):
        for fname in files:
            if fname.lower().endswith(".blend"):
                available[fname.lower()] = os.path.join(root, fname)

    for lib in bpy.data.libraries:
        if not lib.is_missing:
            continue
        lib_name = os.path.basename(lib.filepath).lower()
        if lib_name in available:
            new_path = available[lib_name]
            _log(f"Relinking missing library '{lib.filepath}' → '{new_path}'")
            lib.filepath = new_path
            try:
                lib.reload()
                relinked += 1
                _log(f"Relinked OK: {lib_name}")
            except Exception as e:
                still_missing += 1
                _warn(f"Relink failed for '{lib_name}': {e}")
        else:
            still_missing += 1
            _warn(f"Missing library not found in bundle: {lib.filepath}")

    return relinked, still_missing


# ---------------------------------------------------------------------------
# 4. Pack packable assets
# ---------------------------------------------------------------------------

def _pack_images() -> tuple[int, list[str]]:
    """Pack all external images including UDIM tile sequences."""
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


# ---------------------------------------------------------------------------
# 5. VDB volumes (cannot be packed)
# ---------------------------------------------------------------------------

def _check_volumes() -> list[str]:
    """
    Detect external OpenVDB files. Blender cannot pack VDB volumes into a
    .blend file — they must be present as separate files inside the zip.
    """
    issues = []
    for vol in bpy.data.volumes:
        fp = getattr(vol, "filepath", "")
        if not fp:
            continue
        abs_path = bpy.path.abspath(fp)
        if os.path.exists(abs_path):
            _warn(
                f"External VDB volume '{vol.name}' at '{fp}' — "
                "VDB files cannot be packed. Include this file in your zip archive."
            )
        else:
            _err(
                f"Missing VDB volume '{vol.name}' at '{fp}' — "
                "file not found. Render will fail."
            )
        issues.append(fp)
    return issues


# ---------------------------------------------------------------------------
# 6. Simulation caches (cannot be packed)
# ---------------------------------------------------------------------------

def _check_simulation_caches() -> list[str]:
    """
    Detect baked point caches (cloth, soft body, particles) and fluid caches.
    These live on disk and cannot be embedded — they must be in the zip.
    """
    found = []

    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            # Generic point cache (cloth, soft body, ocean, dynamic paint, etc.)
            cache = getattr(mod, "point_cache", None)
            if cache and getattr(cache, "is_baked", False):
                cache_path = bpy.path.abspath(cache.filepath) if cache.filepath else ""
                label = f"object '{obj.name}' / modifier '{mod.name}'"
                if cache_path and os.path.exists(cache_path):
                    _warn(
                        f"Baked cache at '{cache_path}' ({label}) — "
                        "cannot be packed, must be included in zip."
                    )
                    found.append(cache_path)
                elif cache_path:
                    _err(
                        f"Baked cache MISSING at '{cache_path}' ({label}) — "
                        "simulation will not render correctly."
                    )
                    found.append(cache_path)
                else:
                    _warn(
                        f"Baked cache with unknown disk path for {label} — "
                        "if using disk cache, include the cache folder in zip."
                    )

            # Fluid / smoke domain
            if mod.type == "FLUID":
                try:
                    domain = getattr(mod, "domain_settings", None)
                    if domain:
                        cache_dir = bpy.path.abspath(domain.cache_directory)
                        if os.path.exists(cache_dir):
                            _warn(
                                f"Fluid/smoke cache at '{cache_dir}' (object '{obj.name}') — "
                                "cannot be packed, must be included in zip."
                            )
                            found.append(cache_dir)
                except Exception:
                    pass

        # Particle system caches
        for psys in getattr(obj, "particle_systems", []):
            cache = getattr(psys, "point_cache", None)
            if cache and getattr(cache, "is_baked", False):
                cache_path = bpy.path.abspath(cache.filepath) if cache.filepath else ""
                label = f"object '{obj.name}' / particles '{psys.name}'"
                if cache_path and os.path.exists(cache_path):
                    _warn(
                        f"Baked particle cache at '{cache_path}' ({label}) — "
                        "cannot be packed, must be included in zip."
                    )
                    found.append(cache_path)
                elif not cache_path:
                    _warn(
                        f"Baked particle cache (memory or unknown path) for {label} — "
                        "if using disk cache, include the cache folder in zip."
                    )

    return found


# ---------------------------------------------------------------------------
# 7. Alembic / USD cache files (cannot be packed)
# ---------------------------------------------------------------------------

def _check_alembic_and_usd() -> list[str]:
    """
    Detect external Alembic (.abc) and USD cache files referenced via
    bpy.data.cache_files. These cannot be packed into the .blend.
    """
    issues = []
    for cf in bpy.data.cache_files:
        fp = cf.filepath
        if not fp:
            continue
        abs_path = bpy.path.abspath(fp)
        ext = os.path.splitext(fp)[1].lower()
        kind = "Alembic" if ext == ".abc" else "USD/cache"
        if os.path.exists(abs_path):
            _warn(
                f"External {kind} file '{cf.name}' at '{fp}' — "
                "cannot be packed, must be included in zip."
            )
        else:
            _err(
                f"Missing {kind} file '{cf.name}' at '{fp}' — "
                "render will fail."
            )
        issues.append(fp)
    return issues


# ---------------------------------------------------------------------------
# 8. Python scripts / drivers
# ---------------------------------------------------------------------------

def _check_python_scripts() -> list[str]:
    """
    Warn about text blocks and Python drivers that run automatically via
    --enable-autoexec. Any imported module that is not in standard Blender
    Python (e.g. copy_global_transform, custom addons) will cause a
    ModuleNotFoundError on the render farm.
    """
    flagged = []

    # Text blocks marked as modules (run on load with --enable-autoexec)
    for text in bpy.data.texts:
        if getattr(text, "use_module", False):
            flagged.append(text.name)
            _warn(
                f"Text block '{text.name}' is registered as a Python module "
                "(use_module=True). It will run automatically via --enable-autoexec. "
                "Ensure all imports are available on the render farm."
            )

    # Python expression drivers
    def _has_scripted_drivers(anim_data):
        if not anim_data or not anim_data.drivers:
            return False
        return any(d.driver.type == "SCRIPTED" for d in anim_data.drivers)

    has_py_drivers = any(
        _has_scripted_drivers(getattr(obj, "animation_data", None))
        for obj in bpy.data.objects
    )
    if has_py_drivers:
        _warn(
            "Scene contains Python expression drivers. "
            "These require --enable-autoexec and all imported modules must "
            "be available on render nodes."
        )

    # Linked libraries (their embedded auto-run scripts also execute)
    for lib in bpy.data.libraries:
        if not lib.is_missing:
            lib_name = os.path.basename(lib.filepath)
            _log(
                f"Linked library '{lib_name}' — any embedded auto-run scripts "
                "will execute via --enable-autoexec on the farm."
            )

    return flagged


# ---------------------------------------------------------------------------
# 9. Scene validation
# ---------------------------------------------------------------------------

def _validate_scenes() -> list[str]:
    """Return list of fatal validation errors."""
    errors = []
    active = bpy.context.scene

    if not active.camera:
        cameras = [o for o in active.objects if o.type == "CAMERA"]
        if cameras:
            active.camera = cameras[0]
            _warn(f"No active camera — auto-assigned '{cameras[0].name}'")
        else:
            errors.append(f"Scene '{active.name}' has no camera")
            _err(f"Scene '{active.name}' has no camera — render will fail")

    return errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def prepare():
    filepath = bpy.data.filepath
    if not filepath:
        _err("No blend file loaded")
        sys.exit(1)

    blend_dir = os.path.dirname(filepath)
    _log(f"Blend file: {filepath}")
    _log(f"Blender: {bpy.app.version_string}")

    # 1. Scene report
    for scene in bpy.data.scenes:
        cam = scene.camera.name if scene.camera else "NONE"
        fmt = scene.render.image_settings.file_format
        _log(
            f"Scene '{scene.name}': engine={scene.render.engine}, "
            f"camera={cam}, frames={scene.frame_start}-{scene.frame_end}, "
            f"output_format={fmt}"
        )

    # 2. Fix FFMPEG output
    _fix_output_format()

    # 3. Relink missing libraries
    total_libs = len(list(bpy.data.libraries))
    if total_libs:
        _log(f"Found {total_libs} linked library/libraries — checking...")
        relinked, still_missing = _relink_missing_libraries(blend_dir)
        _log(f"Libraries: {relinked} relinked, {still_missing} still missing")
    else:
        _log("No linked libraries")

    # 4. Pack packable assets
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

    # 5. VDB volumes
    vdb_issues = _check_volumes()
    if vdb_issues:
        _warn(
            f"{len(vdb_issues)} external VDB/volume file(s) detected — "
            "include them in your zip archive alongside the .blend file."
        )

    # 6. Simulation caches
    cache_issues = _check_simulation_caches()
    if cache_issues:
        _warn(
            f"{len(cache_issues)} baked simulation cache(s) detected — "
            "include the cache folders in your zip archive."
        )

    # 7. Alembic / USD
    abc_issues = _check_alembic_and_usd()
    if abc_issues:
        _warn(
            f"{len(abc_issues)} external Alembic/USD cache file(s) detected — "
            "include them in your zip archive."
        )

    # 8. Python scripts / drivers
    _check_python_scripts()

    # 9. Validate
    fatal_errors = _validate_scenes()

    # 10. Save
    bpy.ops.wm.save_as_mainfile(filepath=filepath)
    if fatal_errors:
        _err("Saved with fatal issues — render may fail")
    else:
        _log("Saved prepared blend file")

    print("PREP_DONE", flush=True)


prepare()
