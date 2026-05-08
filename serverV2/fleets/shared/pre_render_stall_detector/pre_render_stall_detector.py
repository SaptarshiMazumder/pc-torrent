"""PreRenderStallDetector — runs the rule list in order, first match wins.

Stateless across jobs.  One instance can evaluate any number of jobs
because rules read their deadlines from ``allowed_stall_times`` on the
row (stamped at dispatch) rather than holding config values at
construction.

Construct via :class:`PreRenderStallDetectorBuilder`.
"""

from __future__ import annotations

from typing import Any, Protocol

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.rules.stall_rule import StallRule
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason


class IPreRenderStallDetector(Protocol):

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        *,
        has_rendered: bool = False,
        allowed_stall_times: dict[str, Any] | None = None,
    ) -> StallReason | None:
        ...


class PreRenderStallDetector:

    def __init__(self, rules: list[StallRule]) -> None:
        self._rules = rules

    def evaluate(
        self,
        window: HeartbeatWindow,
        job_age_sec: float,
        *,
        has_rendered: bool = False,
        allowed_stall_times: dict[str, Any] | None = None,
    ) -> StallReason | None:
        for rule in self._rules:
            reason = rule.evaluate(
                window,
                job_age_sec,
                has_rendered=has_rendered,
                allowed_stall_times=allowed_stall_times,
            )
            if reason is not None:
                return reason
        return None
