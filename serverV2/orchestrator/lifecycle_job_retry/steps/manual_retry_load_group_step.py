"""ManualRetryLoadGroupStep — manual-retry pipeline.

Populates ``ctx.group_id``, ``ctx.chunk_index``, ``ctx.grp`` from
``ctx.raw`` (the user-passed job row).  Raises
``ManualRetryError("group_cancelled")`` if the group is already
cancelled.

The ``not_found`` checks (no row, no group_id) happen in the lifecycle
wrapper BEFORE the pipeline runs, because the resolver needs a valid
``group_id`` to compute anti-affinity.

# SOURCE: lifecycle.py:351-361 (legacy)
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)
from serverV2.orchestrator.lifecycle_job_retry.manual_retry_error import (
    ManualRetryError,
)


class ManualRetryLoadGroupStep:

    def run(self, ctx: RetryContext) -> None:
        ctx.group_id = ctx.raw.get("group_id") or ""
        ctx.chunk_index = ctx.raw.get("chunk_index") or 0
        ctx.grp = ctx.deps.group_repo.get_by_id(ctx.group_id)
        if ctx.grp and ctx.grp.get("status") == "cancelled":
            raise ManualRetryError("group_cancelled")
