"""LoadingStallRule -- pre-first-frame setup phase exceeded its budget.

Catches the "Blender wedged after download, before any frame produced"
case (e.g., GPU texture init failure that hangs silently, OptiX kernel
load loop, EEVEE init deadlock).  The download stall rules don't fire
here because the worker's heartbeats report ``phase != "download"``;
the hard ceiling won't fire for hours.  This rule occupies that gap.

Allowed-time model:

    allowed = clamp(estimated_startup_sec * multiplier, min_sec, max_sec)

``estimated_startup_sec`` is what the planner stamped on the
``PlannedTask`` at allocation time -- a heaviness-aware setup-time
estimate (BVH build + texture VRAM upload + shader compile + Blender
boot).  See ``estimate_startup_seconds`` in
``allocation_strategies/analyzers/allocation_time_analyzer.py``.

``multiplier`` is a global headroom factor: the planner's estimate is a
heuristic, real-world variance is wide, so we wait ``multiplier`` ×
estimate before declaring stall.  Min/max clamp protects against
pathological estimates on very small or very large scenes.

Rule fires only when:

  * the worker has NOT uploaded a single frame yet (``has_rendered``
    is False); once frames are flowing this rule disengages
  * latest heartbeat reports ``phase != "download"`` -- still in
    download is owned by ``BytesStallRule`` / ``DownloadCeilingRule``
  * elapsed-in-loading >= the allowed budget

"Elapsed in loading" is computed by walking the heartbeat window
newest -> oldest, finding the most recent transition out of the
download phase, and treating that timestamp as when loading began.
If the entire window is post-download (the transition happened before
our window of samples), we conservatively use the oldest sample as the
entry point.
"""

from __future__ import annotations

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason


class LoadingStallRule:

    def __init__(
        self, *, multiplier: float, min_sec: float, max_sec: float,
    ) -> None:
        if min_sec > max_sec:
            raise ValueError("LoadingStallRule: min_sec > max_sec")
        self._multiplier = multiplier
        self._min_sec = min_sec
        self._max_sec = max_sec

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        *,
        has_rendered: bool = False,
        estimated_startup_sec: float = 0.0,
        **_unused: object,
    ) -> StallReason | None:
        if has_rendered:
            return None
        latest = window.latest
        if latest is None:
            return None
        if latest.phase == "download":
            return None

        entered_loading_at = self._find_loading_entry(window)
        if entered_loading_at is None:
            return None

        elapsed_in_loading = latest.ts - entered_loading_at
        allowed = max(
            self._min_sec,
            min(self._max_sec, estimated_startup_sec * self._multiplier),
        )
        if elapsed_in_loading < allowed:
            return None

        return StallReason(
            rule="loading_stall",
            message=(
                f"loading phase stuck: {elapsed_in_loading:.0f}s "
                f"> allowed {allowed:.0f}s "
                f"(estimate {estimated_startup_sec:.0f}s "
                f"x {self._multiplier:.1f}, "
                f"clamped to [{self._min_sec:.0f}, {self._max_sec:.0f}])"
            ),
        )

    @staticmethod
    def _find_loading_entry(window: HeartbeatWindow) -> float | None:
        """Return the timestamp at which the worker entered loading
        (i.e., transitioned out of download).  Walks newest -> oldest.
        Returns None when the window is empty."""
        samples = window.samples
        if not samples:
            return None
        last_post_download_ts: float | None = None
        for s in samples:                   # newest -> oldest
            if s.phase == "download":
                # Found the most recent download sample; loading began
                # at the sample we last saw before this one.
                return last_post_download_ts
            last_post_download_ts = s.ts
        # No download samples in the window at all -- loading began
        # before our window.  Use the oldest sample as the conservative
        # entry timestamp.
        return samples[-1].ts
