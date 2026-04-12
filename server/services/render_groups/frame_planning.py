"""
Frame planning — resolve the actual frame range for a render group.

Pure function: takes the payload's requested range, the timeline override in
render_overrides, and the blend-file analysis snapshot; returns the resolved
``(frame_start, frame_end, frame_step, total_frames)`` plus any warnings.

No DB, no HTTP, no storage I/O — callers do the file download and pass the
parsed frame info when the fallback path is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FramePlanResult:
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int


def resolve_frame_range(
    *,
    payload_frame_start: int | None,
    payload_frame_end: int | None,
    payload_frame_step: int | None,
    timeline_overrides: dict[str, Any] | None,
    parsed_frame_info: dict[str, Any] | None = None,
) -> FramePlanResult | None:
    """Determine the frame range for a render group.

    Priority:
    1. Explicit payload values (``payload_frame_start`` / ``payload_frame_end``)
    2. ``timeline_overrides`` from ``render_overrides.timeline``
    3. ``parsed_frame_info`` from blend-file analysis

    Returns ``None`` when none of the three sources provides a complete range
    (the caller should trigger a blend-file parse and call again with
    ``parsed_frame_info``).
    """
    timeline = timeline_overrides if isinstance(timeline_overrides, dict) else {}

    if payload_frame_start is not None and payload_frame_end is not None:
        fs = int(payload_frame_start)
        fe = int(payload_frame_end)
        fstep = int(payload_frame_step or 1)
    elif timeline.get("frame_start") is not None and timeline.get("frame_end") is not None:
        fs = int(timeline["frame_start"])
        fe = int(timeline["frame_end"])
        fstep = int(timeline.get("frame_step") or 1)
    elif parsed_frame_info is not None:
        fs = parsed_frame_info["frame_start"]
        fe = parsed_frame_info["frame_end"]
        fstep = parsed_frame_info["frame_step"]
    else:
        return None

    fstep = max(1, fstep)
    total = ((fe - fs) // fstep) + 1 if fe >= fs else 0
    return FramePlanResult(
        frame_start=fs,
        frame_end=fe,
        frame_step=fstep,
        total_frames=total,
    )
