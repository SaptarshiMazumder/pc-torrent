"""Per-rule modules for PreRenderStallDetector.

Each file owns exactly one rule.  The rules are duck-typed against the
``StallRule`` Protocol declared in :mod:`stall_rule` — no inheritance
required, just a matching ``evaluate`` signature.
"""

from serverV2.fleets.shared.pre_render_stall_detector.rules.bytes_stall_rule import (
    BytesStallRule,
)
from serverV2.fleets.shared.pre_render_stall_detector.rules.cpu_stall_rule import (
    CpuStallRule,
)
from serverV2.fleets.shared.pre_render_stall_detector.rules.download_ceiling_rule import (
    DownloadCeilingRule,
)
from serverV2.fleets.shared.pre_render_stall_detector.rules.hard_ceiling_rule import (
    HardCeilingRule,
)
from serverV2.fleets.shared.pre_render_stall_detector.rules.stall_rule import StallRule

__all__ = [
    "StallRule",
    "CpuStallRule",
    "BytesStallRule",
    "DownloadCeilingRule",
    "HardCeilingRule",
]
