"""DownloadCeilingRule — total time in download phase exceeded a ceiling.

The size-aware ceiling is computed at dispatch and stamped on the
job row as ``allowed_stall_times["download_phase_max_sec"]``.  At
runtime this rule just reads that deadline and compares against
"elapsed in download phase" from the heartbeat window.

When ``allowed_stall_times`` is None / missing the key (legacy rows
pre-dating the column), the rule silently skips.
"""

from __future__ import annotations

from typing import Any

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason


class DownloadCeilingRule:

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
        timeout = allowed_stall_times.get("download_phase_max_sec")
        if timeout is None:
            return None
        timeout = float(timeout)
        latest = window.latest
        if latest is None or latest.phase != "download":
            return None

        oldest_dl = window.oldest_in_phase("download")
        if oldest_dl is None:
            return None
        elapsed = latest.ts - oldest_dl.ts
        if elapsed < timeout:
            return None

        return StallReason(
            rule="download_ceiling",
            message=(
                f"download phase {elapsed:.0f}s exceeded ceiling "
                f"{timeout:.0f}s"
            ),
        )
