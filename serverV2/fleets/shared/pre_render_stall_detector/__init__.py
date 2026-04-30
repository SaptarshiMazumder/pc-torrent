"""pre_render_stall_detector — pre-first-frame liveness detector.

Composes a list of rules (CPU stall, byte stall, download ceiling,
hard ceiling) into a single object the per-job monitors and the
community scanner can consult on every tick.  Construction goes
through :class:`PreRenderStallDetectorBuilder` so callers don't import
the rules directly.

The detector is stateless — all state lives in the heartbeat window
(Redis-backed, survives instance death) and the per-job ``elapsed``
the caller supplies.  Recovery doesn't need a snapshot: rebuild a
fresh detector via the builder, hand it the existing window, evaluate.
"""

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    Heartbeat,
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.pre_render_stall_detector import (
    IPreRenderStallDetector,
    PreRenderStallDetector,
)
from serverV2.fleets.shared.pre_render_stall_detector.pre_render_stall_detector_builder import (
    PreRenderStallDetectorBuilder,
)
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason

__all__ = [
    "Heartbeat",
    "HeartbeatWindow",
    "IPreRenderStallDetector",
    "PreRenderStallDetector",
    "PreRenderStallDetectorBuilder",
    "StallReason",
]
