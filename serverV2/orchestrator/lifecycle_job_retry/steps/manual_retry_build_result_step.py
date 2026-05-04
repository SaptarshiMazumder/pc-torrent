"""ManualRetryBuildResultStep -- manual-retry pipeline.

Final step: composes the dict the API returns to the client.

Since manual retry now parks instead of synchronously dispatching,
the response signals that the retry was queued -- the daemon picks a
target on its next tick.  ``new_job_id`` is None at this point because
no ``jobs`` row has been created yet (the dispatch step does that
later when the daemon promotes the parked row).
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class ManualRetryBuildResultStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.chunk_request is None:
            return
        ctx.result["queued_for_retry"] = bool(ctx.parked)
        ctx.result["chunk_index"] = ctx.chunk_index
        ctx.result["frame_start"] = ctx.chunk_request.frame_start
        ctx.result["frame_end"] = ctx.chunk_request.frame_end
        # No target chosen yet -- daemon picks one on the next tick.
        ctx.result["new_job_id"] = None
        ctx.result["fleet"] = None
        ctx.result["gpu_type"] = None
        # Surface the post-flip group status so the desktop's local
        # state can merge it in -- that re-enables polling for the
        # group, which had been disabled while it was terminal.
        if ctx.group_id:
            grp = ctx.deps.group_repo.get_by_id(ctx.group_id)
            if grp:
                ctx.result["group_status"] = grp.get("status")
