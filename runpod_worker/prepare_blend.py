"""
PC Rent blend file preparation script.
Runs inside Blender (headless) before the actual render.

Usage:
    blender -b {blend_file} -P /scripts/prepare_blend.py

What it does:
  1. Reports scene/camera/engine info
  2. Attempts to relink missing libraries relative to the blend file's directory
  3. Packs all packable external files (images, fonts, sounds, movies)
  4. Reports everything that's still missing
  5. Saves the prepared blend file in place

Output lines are prefixed so handler.py can parse them:
  PREP:       informational
  PREP_WARN:  non-fatal issue (render may have artefacts)
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


def _relink_missing_libraries(blend_dir: str) -> tuple[int, int]:
    """
    Try to find missing library .blend files inside the same directory tree.
    Returns (relinked, still_missing).
    """
    relinked = 0
    still_missing = 0

    # Build a map: filename -> absolute path for every .blend in the bundle
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
            _warn(f"Missing library (not found in bundle): {lib.filepath}")

    return relinked, still_missing


def _pack_images() -> tuple[int, list[str]]:
    packed = 0
    missing = []
    for image in bpy.data.images:
        if image.source not in ("FILE", "SEQUENCE", "MOVIE"):
            continue
        if image.packed_file:
            continue
        try:
            image.pack()
            packed += 1
            _log(f"Packed image: {image.name}")
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


def _validate_scenes() -> list[str]:
    """Return list of fatal validation errors."""
    errors = []
    active = bpy.context.scene

    # Check at least the active scene has a camera
    if not active.camera:
        # Try to find any camera in the scene as fallback
        cameras = [o for o in active.objects if o.type == "CAMERA"]
        if cameras:
            active.camera = cameras[0]
            _warn(f"No active camera set — auto-assigned '{cameras[0].name}'")
        else:
            errors.append(f"Scene '{active.name}' has no camera")
            _err(f"Scene '{active.name}' has no camera — render will fail")

    return errors


def prepare():
    filepath = bpy.data.filepath
    if not filepath:
        _err("No blend file loaded")
        sys.exit(1)

    blend_dir = os.path.dirname(filepath)
    _log(f"Blend file: {filepath}")
    _log(f"Blender: {bpy.app.version_string}")

    # ── 1. Scene/camera/engine report ───────────────────────────────────────
    for scene in bpy.data.scenes:
        cam = scene.camera.name if scene.camera else "NONE"
        engine = scene.render.engine
        f_start, f_end = scene.frame_start, scene.frame_end
        _log(f"Scene '{scene.name}': engine={engine}, camera={cam}, frames={f_start}-{f_end}")

    # ── 2. Relink missing libraries ──────────────────────────────────────────
    total_libs = len(list(bpy.data.libraries))
    if total_libs:
        _log(f"Found {total_libs} linked libraries — checking for missing...")
        relinked, still_missing = _relink_missing_libraries(blend_dir)
        _log(f"Libraries: {relinked} relinked, {still_missing} still missing")
    else:
        _log("No linked libraries")

    # ── 3. Pack assets ───────────────────────────────────────────────────────
    packed_images, missing_images = _pack_images()
    packed_fonts = _pack_fonts()
    packed_sounds = _pack_sounds()

    _log(
        f"Packed: {packed_images} images, {packed_fonts} fonts, {packed_sounds} sounds. "
        f"Unpacked images: {len(missing_images)}"
    )

    if missing_images:
        _warn(f"Missing images (will render pink/black): {', '.join(missing_images[:5])}"
              + (", ..." if len(missing_images) > 5 else ""))

    # ── 4. Scene validation ──────────────────────────────────────────────────
    fatal_errors = _validate_scenes()

    # ── 5. Save ──────────────────────────────────────────────────────────────
    if fatal_errors:
        for e in fatal_errors:
            _err(e)
        # Still save so render.sh gets the partially prepared file
        bpy.ops.wm.save_as_mainfile(filepath=filepath)
        _err("Saved with fatal issues — render may fail")
    else:
        bpy.ops.wm.save_as_mainfile(filepath=filepath)
        _log("Saved prepared blend file")

    print("PREP_DONE", flush=True)


prepare()
