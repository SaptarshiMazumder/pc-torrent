"""CancelProviderStep — RPCs the fleet provider to destroy the
container / cancel the FunctionCall.

Cancel pipeline only.  Failure pipeline doesn't do this -- a failure
signal from the worker / monitor implies the container already died,
so no provider RPC is needed.  (For stall-detected or heartbeat-stale
failures the container may actually still be alive, but the existing
per-fleet monitor's catch-up tick handles that path; preserving the
current ``handle_chunk_failed`` behavior is the explicit ask.)

Best-effort: provider failures are logged but not raised.  The job
row is already terminal in the DB and the monitor (if any) will be
torn down by ``StopMonitorStep`` before this runs, so a botched
provider RPC at worst leaks the container until the provider's own
idle reaper catches up.
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)

log = logging.getLogger(__name__)


class CancelProviderStep:

    def run(self, ctx: TerminationContext) -> None:
        strategy = ctx.deps.fleet_registry.get(ctx.fleet)
        if strategy is None:
            log.warning(
                "cancel %s: no strategy for fleet=%r — provider job not cancelled",
                ctx.job_id, ctx.fleet,
            )
            return
        if not strategy.is_enabled():
            log.warning(
                "cancel %s: fleet %s disabled — provider job not cancelled",
                ctx.job_id, ctx.fleet,
            )
            return
        pid = strategy.provider_job_id_from_job(ctx.raw)
        if not pid:
            log.warning(
                "cancel %s: fleet=%s has no provider_job_id stored — cannot cancel provider-side",
                ctx.job_id, ctx.fleet,
            )
            return
        try:
            strategy.cancel(pid)
            log.info(
                "cancel %s: fleet=%s pid=%s — cancel call returned",
                ctx.job_id, ctx.fleet, pid,
            )
        except Exception as exc:
            log.warning(
                "cancel %s: fleet=%s pid=%s raised %s: %s",
                ctx.job_id, ctx.fleet, pid, type(exc).__name__, exc,
            )
