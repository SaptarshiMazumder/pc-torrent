"""Pre-built termination pipelines.

Two immutable tuples constructed once at import time:

* ``FAILURE_PIPELINE`` -- reproduces ``RenderLifecycle.handle_chunk_failed``
  step-for-step.  No behavior change vs the inline implementation that
  preceded the extraction.

* ``CANCEL_PIPELINE`` -- per-instance cancel.  Adds ``try_retry``,
  ``drain_fleet``, and unconditional ``reconcile_group`` to the
  previous cancel flow so cancelling one worker redirects the chunk
  to a different worker (the documented B2 ask).

Both flows share the same step classes; the difference is which
steps are included and the order.  See termination_pipeline_builder
for the rules-not-action design rationale.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_termination.termination_pipeline_builder import (
    TerminationPipelineBuilder,
)


# ---------------------------------------------------------------------
# FAILURE -- preserves handle_chunk_failed behavior exactly.
#
# Branch trace:
#   1. release_ledger_atomic_cas()          -> sets ctx.we_own_retry
#   2. try_retry(requires_we_own_retry=True)
#        - if !we_own_retry: no-op
#        - else: retry_executor.execute(AUTO_RETRY_PIPELINE, ...)
#               sets ctx.retried from retry_ctx.dispatched
#   3. mark_terminal('failed')              -> mark_failed(job_id, ctx.error)
#   4. release_machine_if_community()
#   5. log_failure_outcome()                -> 1 of 3 messages on flags
#   6. reconcile_group(only_if_not_retried_and_owned=True)
#        - branch A (lost CAS):              skip
#        - branch B + retried:               skip
#        - branch B + not retried:           reconcile
#   7. drain_fleet()                        -> always
# ---------------------------------------------------------------------

FAILURE_PIPELINE = (
    TerminationPipelineBuilder()
    .release_ledger_atomic_cas()
    .try_retry(requires_we_own_retry=True)
    .mark_terminal("failed")
    .release_machine_if_community()
    .log_failure_outcome()
    .reconcile_group(only_if_not_retried_and_owned=True)
    .drain_fleet()
    .build()
)


# ---------------------------------------------------------------------
# CANCEL -- per-instance cancel (B2).
#
# 1. mark_terminal('cancelled', "Cancelled by user")
#      -> first, so CallbackRouter's is_job_terminal short-circuit
#         immediately bounces any straggler signals (heartbeats,
#         late monitor ticks) during the rest of the pipeline.
# 2. release_machine_if_community()
# 3. release_ledger_unconditional()
#      -> also sets we_own_retry=True so try_retry below proceeds
# 4. stop_monitor()                          -> tear down the per-job thread
# 5. cancel_provider()                       -> RPC the provider
# 6. try_retry(requires_we_own_retry=False)  -> re-dispatch missing frames
#                                              to a different worker
# 7. drain_fleet()
# 8. reconcile_group()                       -> always (no gating)
# ---------------------------------------------------------------------

CANCEL_PIPELINE = (
    TerminationPipelineBuilder()
    .mark_terminal("cancelled", error_override="Cancelled by user")
    .release_machine_if_community()
    .release_ledger_unconditional()
    .stop_monitor()
    .cancel_provider()
    .try_retry(requires_we_own_retry=False)
    .drain_fleet()
    .reconcile_group(only_if_not_retried_and_owned=False)
    .build()
)
