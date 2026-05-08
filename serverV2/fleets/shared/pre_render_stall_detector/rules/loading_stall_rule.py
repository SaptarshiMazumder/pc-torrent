"""LoadingStallRule -- pre-first-frame setup phase exceeded its budget.

Catches the "Blender wedged after download, before any frame produced"
case (e.g., GPU texture init failure that hangs silently, OptiX kernel
load loop, EEVEE init deadlock).  The download stall rules don't fire
here because the worker's heartbeats report ``phase != "download"``;
the hard ceiling won't fire for hours.  This rule occupies that gap.

Reads its allowed budget from ``allowed_stall_times["loading_stall_sec"]``
(stamped at dispatch by ``AllowedStallTimesResolver``).  Rule fires
only when:

  * the worker has NOT uploaded a single frame yet (``has_rendered``
    is False); once frames are flowing this rule disengages
  * latest heartbeat reports ``phase != "download"`` -- still in
    download is owned by ``BytesStallRule`` / ``DownloadCeilingRule``
  * elapsed-in-loading >= the allowed budget

"Elapsed in loading" is computed by walking the heartbeat window
newest -> oldest, finding the most recent transition out of the
download phase, and treating that timestamp as when loading began.
"""

from __future__ import annotations

from typing import Any

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason


class LoadingStallRule:

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        *,
        has_rendered: bool = False,
        allowed_stall_times: dict[str, Any] | None = None,
        **_unused: object,
    ) -> StallReason | None:
        if has_rendered:
            return None
        if not allowed_stall_times:
            return None
        allowed = allowed_stall_times.get("loading_stall_sec")
        if allowed is None:
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
        if elapsed_in_loading < float(allowed):
            return None

        return StallReason(
            rule="loading_stall",
            message=(
                f"loading phase stuck: {elapsed_in_loading:.0f}s "
                f"> allowed {float(allowed):.0f}s"
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
                return last_post_download_ts
            last_post_download_ts = s.ts
        return samples[-1].ts
