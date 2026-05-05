"""DownloadCeilingRule — total time in download phase exceeded a ceiling.

The ceiling is size-aware when the worker reports ``total_bytes``:

    timeout = clamp(secs_per_gb * (total_bytes / 1 GiB), min_sec, max_sec)

When ``total_bytes`` is unknown (worker hasn't sent it yet), we fall
back to ``max_sec`` — this rule is the "moving but too slow" backstop;
the bytes_stall rule already catches the "moving zero" case earlier.
"""

from __future__ import annotations

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason

_GIB = 1024 ** 3


class DownloadCeilingRule:

    def __init__(
        self, secs_per_gb: float, min_sec: float, max_sec: float,
    ) -> None:
        if min_sec > max_sec:
            raise ValueError("DownloadCeilingRule: min_sec > max_sec")
        self._secs_per_gb = secs_per_gb
        self._min_sec = min_sec
        self._max_sec = max_sec

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        **_unused: object,
    ) -> StallReason | None:
        latest = window.latest
        if latest is None or latest.phase != "download":
            return None

        oldest_dl = window.oldest_in_phase("download")
        if oldest_dl is None:
            return None
        elapsed = latest.ts - oldest_dl.ts

        timeout = self._timeout_for(latest.total_bytes)
        if elapsed < timeout:
            return None

        return StallReason(
            rule="download_ceiling",
            message=(
                f"download phase {elapsed:.0f}s exceeded ceiling "
                f"{timeout:.0f}s (total_bytes="
                f"{latest.total_bytes if latest.total_bytes is not None else 'unknown'})"
            ),
        )

    def _timeout_for(self, total_bytes: int | None) -> float:
        if total_bytes is None or total_bytes <= 0:
            return self._max_sec
        size_based = (total_bytes / _GIB) * self._secs_per_gb
        return max(self._min_sec, min(self._max_sec, size_based))
