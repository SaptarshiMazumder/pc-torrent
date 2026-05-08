"""BytesStallRule — download phase has been receiving zero bytes.

During ``phase=download``, ``bytes_progressed`` should be monotonically
increasing.  If it doesn't move for ``allowed_stall_times['download_bytes_stall_sec']``
while the worker is still pinging, the TCP/HTTP fetch is wedged — the
worker is alive but the bytes aren't flowing.  Distinct from cpu_stall
(worker-process freeze) and download_ceiling ("moving but too slow").
"""

from __future__ import annotations

from typing import Any

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason

_MIN_OBSERVATION_FRACTION = 0.8


class BytesStallRule:

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
        stall_sec = allowed_stall_times.get("download_bytes_stall_sec")
        if stall_sec is None:
            return None
        stall_sec = float(stall_sec)
        latest = window.latest
        if latest is None or latest.phase != "download":
            return None
        if latest.bytes_progressed is None:
            return None

        # Find the oldest sample within the stall window.  We need at
        # least 0.8 × stall_sec of observation, otherwise we can't
        # conclude bytes haven't moved (window not wide enough yet).
        cutoff_ts = latest.ts - stall_sec
        oldest_in_window = None
        for s in window.samples:
            if s.ts < cutoff_ts:
                break
            oldest_in_window = s
        if oldest_in_window is None:
            return None
        observed = latest.ts - oldest_in_window.ts
        if observed < stall_sec * _MIN_OBSERVATION_FRACTION:
            return None

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
