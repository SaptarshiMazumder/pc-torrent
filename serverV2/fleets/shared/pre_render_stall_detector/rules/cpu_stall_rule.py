"""CpuStallRule — pre-render CPU idle + RSS not growing.

Catches the "container is up, heartbeats are flowing, but the worker
process is wedged" case.  Avg CPU% across the recent window must be
below ``threshold_pct`` AND the process's RSS must not have moved by
more than ``rss_noise_bytes`` (a noisy interpreter touches RSS by
single MB; a stuck process is flat to within a small noise floor).

Skips evaluation in post-render phases — once frames are uploading,
the existing frame-progress staleness check owns liveness.
"""

from __future__ import annotations

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason

_POST_RENDER_PHASES = frozenset({"rendering", "running", "uploading"})
_MIN_SAMPLES = 3


class CpuStallRule:

    def __init__(
        self, threshold_pct: float, window_sec: float, rss_noise_bytes: int,
    ) -> None:
        self._threshold_pct = threshold_pct
        self._window_sec = window_sec
        self._rss_noise_bytes = rss_noise_bytes

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        **_unused: object,
    ) -> StallReason | None:
        latest = window.latest
        if latest is None:
            return None
        if latest.phase in _POST_RENDER_PHASES:
            return None

        recent = window.within_last(self._window_sec)
        if len(recent) < _MIN_SAMPLES:
            return None

        cpus = [s.cpu_percent for s in recent if s.cpu_percent is not None]
        if len(cpus) < _MIN_SAMPLES:
            return None
        avg_cpu = sum(cpus) / len(cpus)
        if avg_cpu >= self._threshold_pct:
            return None

        rss_values = [s.rss_bytes for s in recent if s.rss_bytes is not None]
        if rss_values and (max(rss_values) - min(rss_values)) >= self._rss_noise_bytes:
            return None

        return StallReason(
            rule="cpu_stall",
            message=(
                f"avg CPU {avg_cpu:.1f}% < {self._threshold_pct:.1f}% "
                f"for {self._window_sec:.0f}s and RSS unchanged"
            ),
        )
