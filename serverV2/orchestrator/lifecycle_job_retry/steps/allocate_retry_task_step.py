"""AllocateRetryTaskStep — auto-retry pipeline.

Picks the strategy, calls ``allocate_retry``, and on no-eligible-target
escalates the chunk to ``pending_allocation_queue`` for the daemon to
re-evaluate every tick.  Manual retry uses
``ManualRetryAllocateRetryTaskStep`` instead — same allocation call,
but raises ``ManualRetryError("no_eligible_target")`` on the same
condition (the user gets immediate feedback rather than a silent
escalation).

# SOURCE: retry_dispatcher.py:130-141 (legacy)
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class AllocateRetryTaskStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if ctx.chunk_request is None or ctx.rj is None:
            log.info(
                "[RETRY_DEBUG] AllocateRetryTaskStep: bail (chunk_request=%s, rj=%s)",
                ctx.chunk_request is not None, ctx.rj is not None,
            )
            return
        log.info(
            "[RETRY_DEBUG] AllocateRetryTaskStep(%s): tier=%s file_size=%s frames=%d-%d "
            "exclusions(machine_ids=%d, capabilities=%d) attempt=%d",
            ctx.rj.job_id, ctx.tier, ctx.file_size_bytes,
            ctx.chunk_request.frame_start, ctx.chunk_request.frame_end,
            len(ctx.chunk_request.excluded_machine_ids),
            len(ctx.chunk_request.excluded_serverless_capabilities),
            ctx.chunk_request.attempt,
        )
        retry_task = ctx.deps.allocation_client.plan_retry(
            tier=ctx.tier,
            chunk_request=ctx.chunk_request,
        )
        log.info(
            "[RETRY_DEBUG] AllocateRetryTaskStep(%s): plan_retry returned %s",
            ctx.rj.job_id,
            f"PlannedTask(fleet={retry_task.fleet}, gpu={retry_task.gpu_type}, machine={retry_task.machine_id})"
            if retry_task is not None else "None",
        )
        if retry_task is None:
            log.warning(
                "Job %s: no eligible target for retry of frames %d-%d "
                "(group %s) — escalating to pending_allocation_queue",
                ctx.rj.job_id,
                ctx.chunk_request.frame_start,
                ctx.chunk_request.frame_end,
                ctx.group_id,
            )
            if not ctx.rj.render_overrides_json:
                raise RuntimeError(
                    f"job {ctx.rj.job_id} has no render_overrides_json — cannot escalate retry to pending"
                )
            ctx.deps.allocation_client.enqueue_pending_retry(
                ctx.chunk_request,
                tier=ctx.tier,
                input_filename=ctx.rj.input_filename,
                render_overrides_json=ctx.rj.render_overrides_json,
                max_retries=ctx.rj.max_retries,
                priority=ctx.rj.priority,
            )
            ctx.aborted = True
            return
        ctx.retry_task = retry_task
