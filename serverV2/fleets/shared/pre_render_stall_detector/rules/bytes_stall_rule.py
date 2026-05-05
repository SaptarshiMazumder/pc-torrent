"""BytesStallRule — download phase has been receiving zero bytes.

During ``phase=download``, ``bytes_progressed`` should be monotonically
increasing.  If it doesn't move for ``stall_sec`` while the worker is
still pinging, the TCP/HTTP fetch is wedged — the worker is alive but
the bytes aren't flowing.  Distinct from cpu_stall (which catches
worker-process freeze) and from download_ceiling (which catches "moving
but too slow").
"""

from __future__ import annotations

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason

_MIN_OBSERVATION_FRACTION = 0.8


class BytesStallRule:

    def __init__(self, stall_sec: float) -> None:
        self._stall_sec = stall_sec

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        **_unused: object,
    ) -> StallReason | None:
        latest = window.latest
        if latest is None or latest.phase != "download":
            return None
        if latest.bytes_progressed is None:
            return None

        # Find the oldest sample within the stall window.  We need at least
        # 0.8 × stall_sec of observation, otherwise we can't conclude bytes
        # haven't moved (the window just isn't wide enough yet).
        cutoff_ts = latest.ts - self._stall_sec
        oldest_in_window = None
        for s in window.samples:
            if s.ts < cutoff_ts:
                break
            oldest_in_window = s
        if oldest_in_window is None:
            return None
        observed = latest.ts - oldest_in_window.ts
        if observed < self._stall_sec * _MIN_OBSERVATION_FRACTION:
            return None

        # Earlier sample must also be in download; if the worker entered
        # download more recently than the window, it's a different story.
        if oldest_in_window.phase != "download":
            return None
        if oldest_in_window.bytes_progressed is None:
            return None
        if latest.bytes_progressed != oldest_in_window.bytes_progressed:
            return None

        return StallReason(
            rule="bytes_stall",
            message=(
                f"download bytes_progressed unchanged "
                f"({latest.bytes_progressed} bytes) for {observed:.0f}s"
            ),
        )
