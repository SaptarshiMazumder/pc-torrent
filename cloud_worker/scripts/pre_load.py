"""
PC Rent pre-load script.
Runs inside Blender BEFORE the .blend file is opened (--python before -b).

Ensures that addons embedded auto-run scripts depend on (e.g. cloudrig.py
imports copy_global_transform) are importable when the file loads.
"""

import os
import sys

import bpy


def _find_module_path(blender_dir: str, module_name: str) -> str | None:
    """
    Walk the Blender installation looking for module_name as either:
      - a .py file  (module_name.py)
      - a package   (module_name/__init__.py)
    Returns the directory to add to sys.path, or None if not found.
    """
    py_file = module_name + ".py"
    for root, dirs, files in os.walk(blender_dir):
        # Skip heavy asset / data directories
        dirs[:] = [d for d in dirs if d not in ("datafiles", "locale", "icons")]
        if py_file in files:
            return root
        if module_name in dirs and os.path.isfile(os.path.join(root, module_name, "__init__.py")):
            return root
    return None


def _ensure_importable(module_name: str, blender_dir: str) -> bool:
    """Try to make `import module_name` work. Returns True on success."""
    # 1. Already importable?
    try:
        __import__(module_name)
        print(f"[PRE-LOAD] {module_name} already importable", flush=True)
        return True
    except ImportError:
        pass

    # 2. Find and inject into sys.path
    path = _find_module_path(blender_dir, module_name)
    if path and path not in sys.path:
        sys.path.insert(0, path)
        print(f"[PRE-LOAD] Added to sys.path: {path}", flush=True)
        try:
            __import__(module_name)
            print(f"[PRE-LOAD] {module_name} now importable", flush=True)
            return True
        except ImportError as e:
            print(f"[PRE-LOAD] Still cannot import {module_name}: {e}", flush=True)

    # 3. Try addon_utils as last resort
    try:
        import addon_utils
        for name in (module_name, f"bl_ext.blender_org.{module_name}"):
            try:
                addon_utils.enable(name, default_set=False)
                __import__(module_name)
                print(f"[PRE-LOAD] {module_name} enabled via addon_utils ({name})", flush=True)
                return True
            except Exception:
                pass
    except Exception:
        pass

    print(f"[PRE-LOAD] WARNING: could not make {module_name} importable", flush=True)
    return False


blender_bin = bpy.app.binary_path                    # e.g. /opt/blender/blender
blender_dir = os.path.dirname(blender_bin)           # e.g. /opt/blender

_ensure_importable("copy_global_transform", blender_dir)
