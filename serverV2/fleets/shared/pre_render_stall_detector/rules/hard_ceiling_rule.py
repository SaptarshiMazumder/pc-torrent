"""HardCeilingRule — total monitor age exceeded an absolute ceiling.

Backstop for everything else.  The other rules try to be smart about
what's normal; this one just says "no chunk should ever take more than
N hours."  Applies regardless of phase — even if frames are flowing,
something is wrong if a single chunk hasn't terminated by now.
"""

from __future__ import annotations

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason


class HardCeilingRule:

    def __init__(self, max_sec: float) -> None:
        self._max_sec = max_sec

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        **_unused: object,
    ) -> StallReason | None:
        if job_age_sec < self._max_sec:
            return None
        return StallReason(
            rule="hard_ceiling",
            message=(
                f"monitor age {job_age_sec:.0f}s exceeded hard ceiling "
                f"{self._max_sec:.0f}s"
            ),
        )
