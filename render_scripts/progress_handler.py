import json
import sys

import bpy
from bpy.app.handlers import persistent

PROGRESS_PREFIX = "PCR_PROGRESS "
_state = {
    "total_frames": None,
    "rendered_frames": 0,
}


def emit_progress(kind, scene):
    payload = {
        "kind": kind,
        "current_frame": int(scene.frame_current),
        "rendered_frames": _state["rendered_frames"],
        "total_frames": _state["total_frames"],
    }
    sys.stdout.write(PROGRESS_PREFIX + json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def compute_total_frames(scene):
    start = int(scene.frame_start)
    end = int(scene.frame_end)
    step = max(1, int(scene.frame_step))
    if end < start:
        return 0
    return ((end - start) // step) + 1


def remove_existing_handlers():
    for handler_list in (bpy.app.handlers.render_init, bpy.app.handlers.render_write):
        for handler in list(handler_list):
            if getattr(handler, "__module__", "") == __name__:
                handler_list.remove(handler)


@persistent
def on_render_init(scene, _depsgraph=None):
    _state["total_frames"] = compute_total_frames(scene)
    _state["rendered_frames"] = 0
    emit_progress("meta", scene)


@persistent
def on_render_write(scene, _depsgraph=None):
    total_frames = _state["total_frames"]
    if total_frames is None:
        total_frames = compute_total_frames(scene)
        _state["total_frames"] = total_frames

    next_rendered = _state["rendered_frames"] + 1
    if total_frames and total_frames > 0:
        next_rendered = min(next_rendered, total_frames)
    _state["rendered_frames"] = next_rendered
    emit_progress("frame", scene)


remove_existing_handlers()
bpy.app.handlers.render_init.append(on_render_init)
bpy.app.handlers.render_write.append(on_render_write)
