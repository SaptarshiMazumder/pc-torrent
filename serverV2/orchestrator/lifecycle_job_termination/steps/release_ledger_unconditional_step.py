"""ReleaseLedgerUnconditionalStep — unconditional ledger DELETE for the
cancel pipeline.

Cancel has only one trigger (the user's click), so the atomic-CAS
dedup the failure pipeline needs is unnecessary here.  We just delete
the row outright.

For symmetry with the failure pipeline, this also sets
``ctx.we_own_retry = True`` so downstream steps that gate on it
(``TryRetryStep``, ``ReconcileGroupStep``) treat the cancel as the
authoritative-owner case.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class ReleaseLedgerUnconditionalStep:

    def run(self, ctx: TerminationContext) -> None:
        if ctx.group_id:
            ctx.deps.in_progress_repo.release(ctx.group_id, ctx.chunk_index)
        # Cancel always "owns" the slot for downstream gating purposes.
        ctx.we_own_retry = True
