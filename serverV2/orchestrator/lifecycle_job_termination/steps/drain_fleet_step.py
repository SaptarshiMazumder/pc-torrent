"""DrainFleetStep — kicks off any items queued for the now-freed fleet
slot.

A terminating job frees one of the per-fleet capacity slots; queued
chunks targeting that fleet should now be dispatched.  Best-effort
(``try/except``) so a transient drain failure doesn't fail the
termination pipeline.

Failure pipeline always drained.  Cancel pipeline didn't (small bug);
adding the same step to both flows fixes it.
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)

log = logging.getLogger(__name__)


class DrainFleetStep:

    def run(self, ctx: TerminationContext) -> None:
        if not ctx.fleet:
            return
        try:
            ctx.deps.coordinator.drain_for_fleet(ctx.fleet)
        except Exception as exc:
            log.warning("drain_for_fleet(%s) failed: %s", ctx.fleet, exc)
