"""PreRenderStallDetectorBuilder — fluent assembly of rule sets.

Each ``with_*`` method appends a rule and returns ``self``.  ``build``
freezes the list into an immutable detector.  The builder itself is
disposable; reuse a closure or a factory if you need to build multiple
detectors with the same shape.

Bootstrap typically wraps this in a zero-arg factory:

    def make_pre_render_stall_detector() -> IPreRenderStallDetector:
        return (PreRenderStallDetectorBuilder()
            .with_cpu_stall(threshold_pct=5.0, window_sec=180,
                            rss_noise_bytes=64 * 1024**2)
            .with_bytes_stall(stall_sec=300)
            .with_download_ceiling(secs_per_gb=120,
                                   min_sec=300, max_sec=1800)
            .with_hard_ceiling(max_sec=4 * 3600)
            .build())

so each fleet gets a fresh detector via one call.
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

    def with_bytes_stall(
        self, *, stall_sec: float,
    ) -> "PreRenderStallDetectorBuilder":
        self._rules.append(BytesStallRule(stall_sec=stall_sec))
        return self

    def with_download_ceiling(
        self, *, secs_per_gb: float, min_sec: float, max_sec: float,
    ) -> "PreRenderStallDetectorBuilder":
        self._rules.append(DownloadCeilingRule(
            secs_per_gb=secs_per_gb, min_sec=min_sec, max_sec=max_sec,
        ))
        return self

    def with_hard_ceiling(
        self, *, max_sec: float,
    ) -> "PreRenderStallDetectorBuilder":
        self._rules.append(HardCeilingRule(max_sec=max_sec))
        return self

    def build(self) -> IPreRenderStallDetector:
        return PreRenderStallDetector(list(self._rules))
