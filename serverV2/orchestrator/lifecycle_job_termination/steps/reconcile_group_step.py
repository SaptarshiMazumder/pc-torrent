"""ReconcileGroupStep — recomputes the parent group's overall status.

Two modes via the ``only_if_not_retried_and_owned`` flag:

* False (cancel pipeline default) -- always reconcile.
* True (failure pipeline) -- only reconcile if we won the CAS AND no
  retry was dispatched.  Reproduces the existing
  ``handle_chunk_failed`` behavior, where:
    - branch A (lost CAS / superseded)        -> NO reconcile
    - branch B + retried                       -> NO reconcile
    - branch B + not retried (permanent fail) -> RECONCILE

The reconcile call itself is delegated back to RenderLifecycle through
the ``deps.reconcile_group`` callback, since the rollup logic uses
state that lives on the lifecycle and is shared with success / cancel
paths.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.execution.termination_context import (
    TerminationContext,
)


class ReconcileGroupStep:

    def __init__(self, *, only_if_not_retried_and_owned: bool = False) -> None:
        self._gated = only_if_not_retried_and_owned

    def run(self, ctx: TerminationContext) -> None:
        if self._gated:
            if not ctx.we_own_retry:
                return
            if ctx.retried:
                return
        if ctx.group_id:
            ctx.deps.reconcile_group(ctx.group_id)
