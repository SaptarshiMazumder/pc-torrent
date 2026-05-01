"""ReleaseLedgerAtomicCasStep — atomic dedup primitive used by the
failure pipeline.

DELETE FROM in_progress_chunks
 WHERE group_id = ? AND chunk_index = ? AND current_job_id = ?
RETURNING current_job_id

When N concurrent failure signals fire for the same job, Postgres
serialises the DELETEs and exactly one returns a row.  The winner sets
``ctx.we_own_retry = True`` and proceeds; losers see 0 rows, leave
``we_own_retry = False``, and downstream steps that gate on it
(``TryRetryStep``, ``ReconcileGroupStep`` with the ``not-retried-and-
owned`` flag) become no-ops on their pipeline runs.
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)

log = logging.getLogger(__name__)


class ReleaseLedgerAtomicCasStep:

    def run(self, ctx: TerminationContext) -> None:
        if not ctx.group_id:
            ctx.we_own_retry = False
            return
        ctx.we_own_retry = ctx.deps.in_progress_repo.release_if_owner(
            ctx.group_id, ctx.chunk_index, expected_job_id=ctx.job_id,
        )
