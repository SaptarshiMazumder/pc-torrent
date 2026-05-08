"""PreRenderStallDetectorBuilder — fluent assembly of rule sets.

Each ``with_*`` method appends a rule and returns ``self``.  ``build``
freezes the list into an immutable detector.

After the allowed-stall-times refactor, rules no longer hold
clamp/multiplier values themselves — those live in
``AllowedStallTimesResolver`` and end up on the job row's
``allowed_stall_times`` JSONB column at dispatch.  Rules read their
deadlines from that column at runtime.

The builder still exists as the assembly entry point; ``with_*``
methods just register which rules to include rather than configuring
them.  CpuStallRule is the one rule that still takes config (its
threshold/window/RSS-noise are not deadlines, they're sampling
parameters).
"""

from __future__ import annotations

from serverV2.fleets.shared.pre_render_stall_detector.pre_render_stall_detector import (
    IPreRenderStallDetector,
    PreRenderStallDetector,
)
from serverV2.fleets.shared.pre_render_stall_detector.rules import (
    BytesStallRule,
    CpuStallRule,
    DownloadCeilingRule,
    HardCeilingRule,
    LoadingStallRule,
    StallRule,
)


class PreRenderStallDetectorBuilder:

    def __init__(self) -> None:
        self._rules: list[StallRule] = []

    def with_cpu_stall(
        self, *,
        threshold_pct: float, window_sec: float, rss_noise_bytes: int,
    ) -> "PreRenderStallDetectorBuilder":
        self._rules.append(CpuStallRule(
            threshold_pct=threshold_pct,
            window_sec=window_sec,
            rss_noise_bytes=rss_noise_bytes,
        ))
        return self

    def with_bytes_stall(self) -> "PreRenderStallDetectorBuilder":
        self._rules.append(BytesStallRule())
        return self

    def with_download_ceiling(self) -> "PreRenderStallDetectorBuilder":
        self._rules.append(DownloadCeilingRule())
        return self

    def with_loading_stall(self) -> "PreRenderStallDetectorBuilder":
        self._rules.append(LoadingStallRule())
        return self

    def with_hard_ceiling(self) -> "PreRenderStallDetectorBuilder":
        self._rules.append(HardCeilingRule())
        return self

    def build(self) -> IPreRenderStallDetector:
        return PreRenderStallDetector(list(self._rules))
