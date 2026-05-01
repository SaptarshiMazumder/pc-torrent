"""StopMonitorStep — halts the per-job monitor thread (Vast/Modal).

Cancel pipeline only.  Failure pipeline relies on the monitor's next
tick observing the row's terminal status and exiting on its own --
preserving the existing ``handle_chunk_failed`` behavior, which never
explicitly stopped monitors.

Community has no per-job monitor thread; the registry returns the
community strategy and ``stop_monitoring`` is a no-op there.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class StopMonitorStep:

    def run(self, ctx: TerminationContext) -> None:
        strategy = ctx.deps.fleet_registry.get(ctx.fleet)
        if strategy is not None:
            strategy.stop_monitoring(ctx.job_id)
