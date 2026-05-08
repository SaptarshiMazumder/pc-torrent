"""HardCeilingRule — total monitor age exceeded an absolute ceiling.

Backstop for everything else.  The other rules try to be smart about
what's normal; this one just says "no chunk should ever take more than
N hours."  Applies regardless of phase — even if frames are flowing,
something is wrong if a single chunk hasn't terminated by now.

Reads its ceiling from ``allowed_stall_times["hard_ceiling_sec"]``
(stamped at dispatch).
"""

from __future__ import annotations

from typing import Any

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason


class HardCeilingRule:

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        *,
        allowed_stall_times: dict[str, Any] | None = None,
        **_unused: object,
    ) -> StallReason | None:
        if not allowed_stall_times:
            return None
        max_sec = allowed_stall_times.get("hard_ceiling_sec")
        if max_sec is None:
            return None
        if job_age_sec < float(max_sec):
            return None
        return StallReason(
            rule="hard_ceiling",
            message=(
                f"monitor age {job_age_sec:.0f}s exceeded hard ceiling "
                f"{float(max_sec):.0f}s"
            ),
        )
