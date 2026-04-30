"""PreRenderStallDetector — runs the rule list in order, first match wins.

Stateless across jobs.  One instance can evaluate any number of jobs by
being handed a different ``HeartbeatWindow`` per call — the per-job
``elapsed`` and ``window`` are the only inputs that vary.

Construct via :class:`PreRenderStallDetectorBuilder` rather than calling
the constructor directly so callers don't have to import the rules.
"""

from __future__ import annotations

from typing import Protocol

from serverV2.fleets.shared.pre_render_stall_detector.heartbeat_window import (
    HeartbeatWindow,
)
from serverV2.fleets.shared.pre_render_stall_detector.rules.stall_rule import StallRule
from serverV2.fleets.shared.pre_render_stall_detector.stall_reason import StallReason


class IPreRenderStallDetector(Protocol):

    def evaluate(
        self, window: HeartbeatWindow, job_age_sec: float,
    ) -> StallReason | None:
        ...


class PreRenderStallDetector:

    def __init__(self, rules: list[StallRule]) -> None:
        self._rules = rules

    def evaluate(
        self, window: HeartbeatWindow, job_age_sec: float,
    ) -> StallReason | None:
        for rule in self._rules:
            reason = rule.evaluate(window, job_age_sec)
            if reason is not None:
                return reason
        return None
