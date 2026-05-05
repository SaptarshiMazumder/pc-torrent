"""StallRule — protocol every detector rule implements."""

from __future__ import annotations

from typing import Protocol

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason


class StallRule(Protocol):

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        *,
        has_rendered: bool = False,
        estimated_startup_sec: float = 0.0,
    ) -> StallReason | None:
        """Return a StallReason if the rule trips, None otherwise.

        ``job_age_sec`` is seconds since the monitor began watching this
        job (monotonic for per-job monitors; wallclock for the community
        scanner).  Rules that need a wider context can ignore it.

        ``has_rendered`` indicates whether the worker has uploaded at
        least one frame yet.  Rules that should disengage once frames
        are flowing (e.g. ``LoadingStallRule``) read this; others ignore
        it.

        ``estimated_startup_sec`` is the planner-stamped per-job
        setup-time estimate (BVH build + texture upload + shader
        compile + Blender boot, scaled by scene heaviness).  Used by
        ``LoadingStallRule`` to size its allowed window; other rules
        ignore it.
        """
        ...
