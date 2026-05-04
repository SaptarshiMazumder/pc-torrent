"""ManualRetryBuildResultStep — manual-retry pipeline.

Final step of the manual pipeline: composes the dict the API returns
to the client (new_job_id, frame range, fleet, gpu_type).  ``new_job_id``
was set on ``ctx.result`` by ``EnqueueRetryDispatchStep`` already; this
step adds the rest of the payload.

# SOURCE: lifecycle.py:452-459 (legacy)
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class ManualRetryBuildResultStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.retry_task is None or ctx.chunk_request is None:
            return
        ctx.result["frame_start"] = ctx.chunk_request.frame_start
        ctx.result["frame_end"] = ctx.chunk_request.frame_end
        ctx.result["fleet"] = ctx.retry_task.fleet
        ctx.result["gpu_type"] = ctx.retry_task.gpu_type
        # ``new_job_id`` is already on ctx.result (set by enqueue step);
        # if the dispatch returned no results, ensure the key is present
        # with None so the API response shape is stable.
        ctx.result.setdefault("new_job_id", None)
        # Surface the post-flip group status so the desktop's local
        # state can merge it in — that re-enables polling for the
        # group, which had been disabled while it was terminal.
        # Read fresh from the repo so we capture the flip-step's write.
        if ctx.group_id:
            grp = ctx.deps.group_repo.get_by_id(ctx.group_id)
            if grp:
                ctx.result["group_status"] = grp.get("status")
