"""StallRule — protocol every detector rule implements."""

from __future__ import annotations

from typing import Any, Protocol

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
        allowed_stall_times: dict[str, Any] | None = None,
    ) -> StallReason | None:
        """Return a StallReason if the rule trips, None otherwise.

        ``job_age_sec`` is wallclock seconds since job dispatch.

        ``has_rendered`` indicates whether the worker has uploaded at
        least one frame yet.  Rules that should disengage once frames
        are flowing (e.g. ``LoadingStallRule``) read this; others
        ignore it.

        ``allowed_stall_times`` is the resolved-deadline dict written
        to ``jobs.allowed_stall_times`` at dispatch.  Rules read their
        own deadline key from this dict (e.g. ``LoadingStallRule``
        reads ``loading_stall_sec``).  None / missing key → rule
        skips (returns None) so legacy rows pre-dating the column
        gracefully no-op.
        """
        ...
