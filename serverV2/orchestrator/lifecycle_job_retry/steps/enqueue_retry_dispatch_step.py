"""EnqueueRetryDispatchStep — shared between auto and manual pipelines.

Builds the ``DispatchContext`` from the resolved ``RenderJob`` and asks
the coordinator to enqueue + flush the retry task.  Stores the dispatch
results on ``ctx`` for downstream steps (manual retry uses them to
build its return payload).

# SOURCE: retry_dispatcher.py:152-167 (legacy auto)
# SOURCE: lifecycle.py:433-446 (legacy manual)
"""

from __future__ import annotations

from serverV2.core.models import DispatchContext
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class EnqueueRetryDispatchStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if ctx.retry_task is None or ctx.rj is None:
            return
        if not ctx.rj.render_overrides_json:
            raise RuntimeError(
                f"job {ctx.rj.job_id} has no render_overrides_json — cannot retry"
            )
        dispatch_context = DispatchContext(
            group_id=ctx.group_id,
            input_filename=ctx.rj.input_filename,
            render_overrides_json=ctx.rj.render_overrides_json,
            blend_url="",
            max_retries=ctx.rj.max_retries,
            priority=ctx.rj.priority,
            engine=ctx.engine,
        )
        results = ctx.deps.coordinator.enqueue_and_flush(
            ctx.group_id, [ctx.retry_task], dispatch_context,
        )
        ctx.dispatched = True
        # Stash the dispatch result for any downstream step that needs
        # the new job_id (manual retry's result builder).
        if results:
            ctx.result.setdefault("new_job_id", results[0].job_id)
