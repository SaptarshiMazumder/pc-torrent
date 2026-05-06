"""ReleaseMachineStep — flips the community machine back to
``available`` so the allocator can return it for the next dispatch.

No-op on Vast/Modal jobs (they have no machines row).  Goes through
``MachineRepository.update_status`` which writes PG sync and mirrors
to Redis on a background executor.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class ReleaseMachineStep:

    def run(self, ctx: TerminationContext) -> None:
        if ctx.fleet == "windows" and ctx.machine_id:
            ctx.deps.machine_repo.update_status(ctx.machine_id, "available")
