"""ParkRetryToPendingStep -- the only retry-side write to the queues.

Both pipelines (auto + manual) end here.  Instead of synchronously
planning a target and writing to ``dispatch_queue``, the step parks the
chunk request on ``pending_allocation_queue``.  The dispatch daemon's
per-tick re-evaluator picks it up, plans against its tick-local mutable
snapshot, and -- only on success -- promotes the row to
``dispatch_queue``.

Why parking instead of synchronous plan + enqueue:

* Auto-retry callbacks fire from per-job monitor threads (Vast, Modal).
  If they read the snapshot cache and plan synchronously, they race
  with the daemon's mid-tick mutations -- the cache only persists at
  end-of-tick, so a callback firing mid-tick sees pre-tick state and
  can pick a target the daemon has already consumed.
* Manual-retry runs on the HTTP request thread; same race.

By parking, the only thread reading or writing fleet availability is
the daemon.  No cross-thread cache coherence problem possible.

Sets ``ctx.parked = True`` so downstream log + result steps know work
was queued asynchronously; ``ctx.dispatched`` stays False (nothing was
dispatched yet).
"""

from __future__ import annotations

import logging

from serverV2.core.models import DispatchContext
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class ParkRetryToPendingStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if ctx.chunk_request is None or ctx.rj is None:
            return
        if not ctx.rj.render_overrides_json:
            raise RuntimeError(
                f"job {ctx.rj.job_id} has no render_overrides_json -- cannot retry"
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
        ctx.deps.allocation_client.park_retry(
            chunk_request=ctx.chunk_request,
            tier=ctx.tier,
            dispatch_context=dispatch_context,
        )
        ctx.parked = True
