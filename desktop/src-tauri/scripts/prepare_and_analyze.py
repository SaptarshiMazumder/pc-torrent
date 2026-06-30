"""
Forge merged blend preparation + analysis script.
Runs inside Blender (headless) as a SINGLE invocation -- prepare()
followed by analyze() in the same Blender session.  Replaces the
old two-step prepare_blend.py + analyze_blend.py flow, eliminating
one full Blender startup + .blend load.

Output prefixes parsed by the Tauri host (desktop/src-tauri/src/commands.rs):
  PREP:               informational
  PREP_WARN:          non-fatal issue
  PREP_ERROR:         fatal issue
  PREP_DONE           prepare phase finished (final pack + save successful)
  PHASE:<name>:start  phase begin
  PHASE:<name>:end:<seconds>   phase end with elapsed wall-time
  PROGRESS:<phase>:<n>/<total>:<label>   sub-progress within a long phase
  PCR_ANALYSIS_JSON:  final analysis snapshot (single-line JSON)
"""

import bpy
import json
import os
import platform
import re
import string
import sys
import time
import traceback


# ── Unbuffered output ─────────────────────────────────────────────────────────
# When Blender's stdout is piped to a parent process (as it is here from
# the Tauri host), the C-level pipe is block-buffered by default on
# Windows.  Plain ``print(..., flush=True)`` only flushes Python's
# TextIOWrapper buffer -- the underlying file-descriptor write still
# waits for the buffer to fill, so PHASE/PROGRESS lines arrive as one
# burst when Blender exits instead of streaming.  ``reconfigure(write_through)``
# (Python 3.7+) bypasses the FD buffer on every write.
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    print(f"PREP: {msg}", flush=True)


def _warn(msg: str):
    print(f"PREP_WARN: {msg}", flush=True)


def _err(msg: str):
    print(f"PREP_ERROR: {msg}", flush=True)


def _phase_start(name: str):
    print(f"PHASE:{name}:start", flush=True)


def _phase_end(name: str, t0: float):
    print(f"PHASE:{name}:end:{time.monotonic() - t0:.3f}", flush=True)


def _progress(phase: str, n: int, total: int, label: str = ""):
    print(f"PROGRESS:{phase}:{n}/{total}:{label}", flush=True)


def _safe(label: str, fn, *args):
    """Run one advisory step without aborting the pipeline.
    Returns the step's value, or None on failure.
    """
    try:
        return fn(*args)
    except Exception as e:
        _warn(f"Step '{label}' failed (non-fatal): {e}")
        _warn("Trace: " + traceback.format_exc().strip().replace("\n", " | "))
        return None


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
    try:
        bpy.ops.file.make_paths_relative()
        _log("All file paths remapped to relative")
    except Exception as e:
        _warn(f"Could not make paths relative: {e}")


# ── 3b. Recover missing external file references (tiered, opt-in) ────────────

_RECOVER_TOTAL_BUDGET_SEC = 90.0
_DRIVE_FIXED = 3


def _count_missing_refs() -> int:
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


_NOISE_DIRS = frozenset(name.lower() for name in (
    "$Recycle.Bin", "System Volume Information", "Recovery",
    "Windows", "Windows.old", "WinSxS",
    "Program Files", "Program Files (x86)", "ProgramData",
    "PerfLogs", "MSOCache",
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100",
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv",
    ".cache", ".tox",
))


def _missing_basenames() -> set[str]:
    names: set[str] = set()

    def _add(item, packed_attr: str = "packed_file") -> None:
        fp = getattr(item, "filepath", "")
        if not fp:
            return
        if packed_attr and getattr(item, packed_attr, None):
            return
        try:
            if os.path.exists(bpy.path.abspath(fp)):
                return
        except Exception:
            return
        base = os.path.basename(fp.replace("\\", "/")).lower()
        if base:
            names.add(base)

    for img in bpy.data.images:
        if img.source in ("FILE", "SEQUENCE", "MOVIE", "TILED"):
            _add(img)
    for lib in bpy.data.libraries:
        if getattr(lib, "is_missing", False):
            base = os.path.basename(lib.filepath.replace("\\", "/")).lower()
            if base:
                names.add(base)
    for font in bpy.data.fonts:
        if font.filepath not in ("<builtin>", ""):
            _add(font)
    for snd in bpy.data.sounds:
        _add(snd)
    for clip in bpy.data.movieclips:
        _add(clip, packed_attr="")
    for vol in bpy.data.volumes:
        _add(vol, packed_attr="")
    for cf in bpy.data.cache_files:
        _add(cf, packed_attr="")
    return names


def _apply_found_paths(found: dict[str, str]) -> None:
    def _maybe_apply(item, packed_attr: str = "packed_file") -> bool:
        fp = getattr(item, "filepath", "")
        if not fp:
            return False
        if packed_attr and getattr(item, packed_attr, None):
            return False
        try:
            if os.path.exists(bpy.path.abspath(fp)):
                return False
        except Exception:
            return False
        base = os.path.basename(fp.replace("\\", "/")).lower()
        new_path = found.get(base)
        if not new_path:
            return False
        item.filepath = new_path
        return True

    for img in bpy.data.images:
        if img.source not in ("FILE", "SEQUENCE", "MOVIE", "TILED"):
            continue
        if _maybe_apply(img):
            try:
                img.reload()
            except Exception:
                pass
    for lib in bpy.data.libraries:
        if not getattr(lib, "is_missing", False):
            continue
        base = os.path.basename(lib.filepath.replace("\\", "/")).lower()
        new_path = found.get(base)
        if not new_path:
            continue
        lib.filepath = new_path
        try:
            lib.reload()
        except Exception:
            pass
    for font in bpy.data.fonts:
        if font.filepath in ("<builtin>", ""):
            continue
        _maybe_apply(font)
    for snd in bpy.data.sounds:
        _maybe_apply(snd)
    for clip in bpy.data.movieclips:
        _maybe_apply(clip, packed_attr="")
    for vol in bpy.data.volumes:
        _maybe_apply(vol, packed_attr="")
    for cf in bpy.data.cache_files:
        _maybe_apply(cf, packed_attr="")


def _try_find_under(directory: str, label: str, time_left: float) -> int:
    if time_left <= 0 or not directory:
        return 0
    if not os.path.isdir(directory):
        return 0
    needed = _missing_basenames()
    if not needed:
        return 0

    deadline = time.monotonic() + time_left
    t_start = time.monotonic()
    found: dict[str, str] = {}
    dirs_visited = 0
    budget_exhausted = False

    try:
        walker = os.walk(directory, topdown=True, onerror=lambda _e: None)
        for root, dirs, files in walker:
            dirs_visited += 1
            if time.monotonic() >= deadline:
                budget_exhausted = True
                break
            dirs[:] = [d for d in dirs if d.lower() not in _NOISE_DIRS]
            for fname in files:
                low = fname.lower()
                if low in needed and low not in found:
                    found[low] = os.path.join(root, fname)
                    needed.discard(low)
                    if not needed:
                        break
            if not needed:
                break
    except Exception as exc:
        _warn(f"walk under {label} failed: {exc}")

    elapsed = time.monotonic() - t_start
    if found:
        _apply_found_paths(found)
    resolved = len(found)
    suffix = " (budget exhausted)" if budget_exhausted else ""
    _log(
        f"search {label}: resolved {resolved} reference(s) in {elapsed:.1f}s "
        f"after visiting {dirs_visited} directories{suffix}"
    )
    return resolved


def _recover_missing_files(blend_dir: str) -> int:
    initial = _count_missing_refs()
    if initial == 0:
        return 0
    _log(f"Recovering {initial} missing external reference(s) -- tiered search")
    deadline = time.monotonic() + _RECOVER_TOTAL_BUDGET_SEC
    total_resolved = 0

    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    total_resolved += _try_find_under(blend_dir, "tier1 (blend dir)", _remaining())
    if _count_missing_refs() == 0:
        return total_resolved

    parent_dir = os.path.dirname(blend_dir.rstrip("\\/")) if blend_dir else ""
    if parent_dir and parent_dir != blend_dir:
        total_resolved += _try_find_under(parent_dir, "tier2 (parent dir)", _remaining())
        if _count_missing_refs() == 0:
            return total_resolved

    home = os.path.expanduser("~")
    if home and home != blend_dir and home != parent_dir:
        total_resolved += _try_find_under(home, "tier3 (user home)", _remaining())
        if _count_missing_refs() == 0:
            return total_resolved

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

    available: dict[str, list[str]] = {}
    for root, _, files in os.walk(blend_dir):
        for fname in files:
            if fname.lower().endswith(".blend"):
                available.setdefault(fname.lower(), []).append(
                    os.path.join(root, fname)
                )

    libs = [lib for lib in bpy.data.libraries if lib.is_missing]
    total = len(libs)
    for i, lib in enumerate(libs, start=1):
        _progress("relink_libs", i, total, os.path.basename(lib.filepath))
        lib_name = os.path.basename(lib.filepath).lower()
        candidates = available.get(lib_name, [])
        if not candidates:
            still_missing += 1
            _warn(f"Missing library not found in bundle: {lib.filepath}")
            continue
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
    import addon_utils
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
        try:
            __import__(mod_name)
            continue
        except ImportError:
            pass
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
    targets = [
        img for img in bpy.data.images
        if img.source in ("FILE", "SEQUENCE", "MOVIE", "TILED")
        and not img.packed_file
    ]
    total = len(targets)
    packed = 0
    missing: list[str] = []
    for i, image in enumerate(targets, start=1):
        _progress("pack_images", i, total, image.name)
        try:
            image.pack()
            packed += 1
            _log(f"Packed image: {image.name} (source={image.source})")
        except Exception as e:
            missing.append(image.filepath or image.name)
            _warn(f"Cannot pack image '{image.name}' ({image.filepath}): {e}")
    return packed, missing


def _pack_fonts() -> int:
    targets = [
        f for f in bpy.data.fonts
        if not f.packed_file and f.filepath not in ("<builtin>", "")
    ]
    total = len(targets)
    packed = 0
    for i, font in enumerate(targets, start=1):
        _progress("pack_fonts", i, total, font.name)
        try:
            font.pack()
            packed += 1
            _log(f"Packed font: {font.name}")
        except Exception as e:
            _warn(f"Cannot pack font '{font.name}': {e}")
    return packed


def _pack_sounds() -> int:
    targets = [s for s in bpy.data.sounds if not s.packed_file]
    total = len(targets)
    packed = 0
    for i, sound in enumerate(targets, start=1):
        _progress("pack_sounds", i, total, sound.name)
        try:
            sound.pack()
            packed += 1
            _log(f"Packed sound: {sound.name}")
        except Exception as e:
            _warn(f"Cannot pack sound '{sound.name}': {e}")
    return packed


def _check_movieclips() -> list[str]:
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
    issues = []
    for scene in bpy.data.scenes:
        node_tree = getattr(scene, "compositing_node_group", None)
        if node_tree is None:
            if not getattr(scene, "use_nodes", False):
                continue
            node_tree = getattr(scene, "node_tree", None)
        if not node_tree:
            continue
        for node in node_tree.nodes:
            if node.type == "MOVIECLIP" and node.clip:
                fp = node.clip.filepath
                if fp:
                    abs_path = bpy.path.abspath(fp)
                    if not os.path.exists(abs_path):
                        _warn(f"Compositor MovieClip node '{node.name}' references missing file: {fp}")
                        issues.append(fp)
            if node.type == "IMAGE" and node.image:
                img = node.image
                if img.source in ("FILE", "SEQUENCE", "MOVIE", "TILED") and not img.packed_file:
                    abs_path = bpy.path.abspath(img.filepath)
                    if not os.path.exists(abs_path):
                        _warn(f"Compositor Image node '{node.name}' references unpacked missing image: {img.filepath}")
                        issues.append(img.filepath)
    return issues


def _check_vse_strips() -> list[str]:
    issues = []
    for scene in bpy.data.scenes:
        se = scene.sequence_editor
        if not se:
            continue
        strips = getattr(se, "strips_all", None)
        if strips is None:
            strips = getattr(se, "sequences_all", [])
        for strip in strips:
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
    scene = bpy.context.scene
    start = int(scene.frame_start)
    end = int(scene.frame_end)

    drivers_to_bake: list[tuple] = []
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

    total_drivers = len(drivers_to_bake)
    frame_span = end - start + 1
    _log(f"Baking {total_drivers} scripted driver(s) across frames {start}-{end}...")

    samples: dict[tuple, list[tuple[int, float]]] = {}

    # Outer loop: frames.  Per-frame depsgraph update is the expensive part,
    # so reporting per-frame progress (collapsed onto the "bake_drivers"
    # phase) is the most useful signal for the UI.
    for frame_idx, frame in enumerate(range(start, end + 1), start=1):
        scene.frame_set(frame)
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
        # Only emit every ~5% to avoid flooding stdout on long animations.
        if frame_span < 20 or frame_idx % max(1, frame_span // 20) == 0 or frame_idx == frame_span:
            _progress(
                "bake_drivers",
                frame_idx,
                frame_span,
                f"frame {frame}/{end} · {total_drivers} drivers",
            )

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

    _log(f"Baked {baked}/{total_drivers} scripted drivers to keyframes")
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
            all_cameras = [o for o in bpy.data.objects if o.type == "CAMERA"]
            if all_cameras:
                active.camera = all_cameras[0]
                _warn(f"No camera in active scene — assigned '{all_cameras[0].name}' from another scene")
            else:
                errors.append(f"Scene '{active.name}' has no camera")
                _err(f"Scene '{active.name}' has no camera — render will fail")
    return errors


# ── Prepare driver ────────────────────────────────────────────────────────────

def prepare():
    filepath = bpy.data.filepath
    if not filepath:
        _err("No blend file loaded")
        sys.exit(1)

    blend_dir = os.path.dirname(filepath)
    _log(f"Blend file: {filepath}")
    _log(f"Blender: {bpy.app.version_string}")

    def _scene_report():
        for scene in bpy.data.scenes:
            cam = scene.camera.name if scene.camera else "NONE"
            fmt = scene.render.image_settings.file_format
            engine = scene.render.engine
            _log(
                f"Scene '{scene.name}': engine={engine}, camera={cam}, "
                f"frames={scene.frame_start}-{scene.frame_end}, output_format={fmt}"
            )

    # Each phase wraps its work in PHASE:start/end so the UI can render
    # accurate percent + ETA.  _safe() guards against API breakage in
    # any advisory step.

    t = time.monotonic(); _phase_start("scene_report")
    _safe("scene report", _scene_report)
    _phase_end("scene_report", t)

    t = time.monotonic(); _phase_start("fix_output")
    _safe("fix output format", _fix_output_format)
    _phase_end("fix_output", t)

    t = time.monotonic(); _phase_start("relative_paths")
    _safe("make paths relative", _make_paths_relative)
    _phase_end("relative_paths", t)

    if os.environ.get("PCR_DEEP_SEARCH") == "1":
        t = time.monotonic(); _phase_start("deep_search")
        _safe("recover missing files", _recover_missing_files, blend_dir)
        _phase_end("deep_search", t)
    else:
        _log("Deep search disabled (default).")

    t = time.monotonic(); _phase_start("relink_libs")
    total_libs = len(list(bpy.data.libraries))
    if total_libs:
        _log(f"Found {total_libs} linked library/libraries — checking...")
        relink_result = _safe("relink libraries", _relink_missing_libraries, blend_dir)
        if relink_result:
            relinked, still_missing = relink_result
            _log(f"Libraries: {relinked} relinked, {still_missing} still missing")
    else:
        _log("No linked libraries")
    _phase_end("relink_libs", t)

    t = time.monotonic(); _phase_start("enable_addons")
    _safe("enable addons", _enable_required_addons)
    _phase_end("enable_addons", t)

    t = time.monotonic(); _phase_start("pack_images")
    packed_images, missing_images = _pack_images()
    _phase_end("pack_images", t)

    t = time.monotonic(); _phase_start("pack_fonts")
    packed_fonts = _pack_fonts()
    _phase_end("pack_fonts", t)

    t = time.monotonic(); _phase_start("pack_sounds")
    packed_sounds = _pack_sounds()
    _phase_end("pack_sounds", t)

    _log(
        f"Packed: {packed_images} images, {packed_fonts} fonts, {packed_sounds} sounds. "
        f"Unpacked images: {len(missing_images)}"
    )
    if missing_images:
        preview = ", ".join(missing_images[:5])
        extra = ", ..." if len(missing_images) > 5 else ""
        _warn(f"Missing images (will render pink/black): {preview}{extra}")

    t = time.monotonic(); _phase_start("check_external")
    clip_issues = _safe("check movie clips", _check_movieclips) or []
    if clip_issues:
        _warn(f"{len(clip_issues)} movie clip(s) cannot be packed — include in zip.")
    vdb_issues = _safe("check volumes", _check_volumes) or []
    if vdb_issues:
        _warn(f"{len(vdb_issues)} external VDB/volume file(s) — include in zip.")
    cache_issues = _safe("check simulation caches", _check_simulation_caches) or []
    if cache_issues:
        _warn(f"{len(cache_issues)} baked simulation cache(s) — include cache folders in zip.")
    abc_issues = _safe("check alembic/usd", _check_alembic_and_usd) or []
    if abc_issues:
        _warn(f"{len(abc_issues)} external Alembic/USD cache file(s) — include in zip.")
    _safe("check compositor", _check_compositor)
    _safe("check vse strips", _check_vse_strips)
    _phase_end("check_external", t)

    t = time.monotonic(); _phase_start("bake_drivers")
    _safe("bake scripted drivers", _bake_scripted_drivers)
    _phase_end("bake_drivers", t)

    t = time.monotonic(); _phase_start("validate")
    _safe("validate scenes", _validate_scenes)
    _phase_end("validate", t)

    t = time.monotonic(); _phase_start("save")
    output_path = os.environ["PCR_PREP_OUTPUT_PATH"]
    bpy.ops.wm.save_as_mainfile(filepath=output_path)
    _log(f"Saved prepared blend file to {output_path}")
    _phase_end("save", t)
    print("PREP_DONE", flush=True)


# ── Analyze (post-prepare; reads in-memory packed state) ─────────────────────

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
    t = time.monotonic(); _phase_start("analyze_scene")
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
    _phase_end("analyze_scene", t)

    t = time.monotonic(); _phase_start("analyze_heaviness")
    heaviness = {}
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
        # Per-material shader walk is the most expensive sub-step on
        # heavy scenes -- emit per-5% progress so the bar moves.
        mat_total = len(active_materials)
        progress_step = max(1, mat_total // 20)
        for i, mat in enumerate(active_materials, start=1):
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
            if i % progress_step == 0 or i == mat_total:
                _progress("analyze_heaviness", i, mat_total, f"{mat_total} materials")
            if uses_volumetrics and uses_subsurface_scattering:
                # Done early -- still want a final 100% tick for the UI.
                _progress("analyze_heaviness", mat_total, mat_total, f"{mat_total} materials")
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
    _phase_end("analyze_heaviness", t)

    # ``finalizing`` covers the time between handing off the analysis JSON
    # and Blender actually exiting -- on heavy scenes Blender's cleanup
    # (releasing the data graph, freeing GPU resources, writing logs)
    # can take many seconds; without this phase the UI shows the heaviness
    # phase frozen at 100% while the process is still very much alive.
    # No phase_end is emitted: the process exit is the implicit terminator.
    _phase_start("finalizing")
    print("PCR_ANALYSIS_JSON:" + json.dumps(payload, separators=(",", ":")), flush=True)


prepare()
analyze()
