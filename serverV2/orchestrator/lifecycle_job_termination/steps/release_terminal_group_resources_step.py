"""ReleaseTerminalGroupResourcesStep -- after reconcile_group has run,
drain the dispatch + pending allocation queues if the group flipped
terminal during this pipeline.

The actual logic lives in ``TerminalGroupResourceReleaser``; this step
is a thin pipeline adapter that calls it via the
``deps.release_terminal_group_resources`` callable.  The releaser is
self-gating, so this step is unconditional -- it always runs, and the
releaser no-ops when the group isn't terminal.

Place after ``ReconcileGroupStep`` so the group-status flip (if any)
is already persisted by the time we read it.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class ReleaseTerminalGroupResourcesStep:

    def run(self, ctx: TerminationContext) -> None:
        if ctx.group_id:
            ctx.deps.release_terminal_group_resources(ctx.group_id)
