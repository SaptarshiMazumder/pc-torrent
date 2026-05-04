"""SubmitRetryStep — auto-retry pipeline.

One step, one allocation-side call.  Replaces the old
``AllocateRetryTaskStep`` + ``EnqueueRetryDispatchStep`` split — the
allocation module now owns the plan-vs-park decision internally, and
the retry pipeline just feeds it the chunk request + dispatch context
and inspects the result.

On park (no eligible target right now), sets ``ctx.aborted=True`` so
the rest of the pipeline short-circuits.  The pending row will be
re-evaluated on every daemon tick; the group-status aggregator treats
it as still active (``has_pending_allocation=True``) so the group
doesn't flip to ``failed`` while parked.

Manual retry uses its own raise-on-no-target step
(``ManualRetryAllocateRetryTaskStep`` + ``ManualRetryEnqueueDispatch
Step``) — manual retry shouldn't silently park.
"""

from __future__ import annotations

import logging

from serverV2.core.models import DispatchContext
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class SubmitRetryStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if ctx.chunk_request is None or ctx.rj is None:
            log.info(
                "[RETRY_DEBUG] SubmitRetryStep: bail (chunk_request=%s, rj=%s)",
                ctx.chunk_request is not None, ctx.rj is not None,
            )
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
        log.info(
            "[RETRY_DEBUG] SubmitRetryStep(%s): tier=%s file_size=%s frames=%d-%d "
            "exclusions(machine_ids=%d, capabilities=%d) attempt=%d",
            ctx.rj.job_id, ctx.tier, ctx.file_size_bytes,
            ctx.chunk_request.frame_start, ctx.chunk_request.frame_end,
            len(ctx.chunk_request.excluded_machine_ids),
            len(ctx.chunk_request.excluded_serverless_capabilities),
            ctx.chunk_request.attempt,
        )
        result = ctx.deps.allocation_client.submit_retry(
            chunk_request=ctx.chunk_request,
            tier=ctx.tier,
            dispatch_context=dispatch_context,
        )
        if result.parked:
            log.warning(
                "Job %s: no eligible target for retry of frames %d-%d "
                "(group %s) — parked on pending_allocation_queue",
                ctx.rj.job_id,
                ctx.chunk_request.frame_start,
                ctx.chunk_request.frame_end,
                ctx.group_id,
            )
            ctx.aborted = True
            return
        log.info(
            "[RETRY_DEBUG] SubmitRetryStep(%s): dispatched job_id=%s status=%s",
            ctx.rj.job_id,
            result.dispatch_result.job_id if result.dispatch_result else "<none>",
            result.dispatch_result.status if result.dispatch_result else "<none>",
        )
        ctx.retry_task = result.planned
        ctx.dispatched = True
        if result.dispatch_result is not None:
            ctx.result.setdefault("new_job_id", result.dispatch_result.job_id)
