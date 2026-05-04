"""Pre-built retry pipelines.

Two immutable tuples constructed once at import time:

* ``AUTO_RETRY_PIPELINE`` -- reproduces the legacy
  ``RetryDispatcher.attempt`` step-for-step.  Silent short-circuit on
  abort (matches ``return False``).  Used by the failure / cancel
  termination pipelines via ``TryRetryStep``.

* ``MANUAL_RETRY_PIPELINE`` -- reproduces the legacy
  ``RenderLifecycle.retry_chunk_manually`` step-for-step.  Raises
  ``ManualRetryError`` on any refusal (matches the typed-error API
  contract surfaced by the router).

Both flows share most steps.  The differences are:
* Manual retry's row-resolution path (latest sibling lookup) vs auto's
  direct-from-input.
* Manual retry's ``attempt = 0`` reset vs auto's ``attempt = N + 1``
  with MAX_RETRIES enforcement.
* Manual retry's typed-error refusal vs auto's silent log + abort.
* Manual retry's terminal reconcile + result-building (so the API can
  return ``new_job_id`` etc.).

See ``retry_pipeline_builder`` for the rules-not-action design rationale.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.retry_pipeline_builder import (
    RetryPipelineBuilder,
)


# ---------------------------------------------------------------------
# AUTO -- preserves RetryDispatcher.attempt behavior exactly.
#
# Step trace (legacy retry_dispatcher.py:80-167):
#   1. load_job_and_group()           -> ctx.rj, group_id, chunk_index, grp
#   2. abort_if_group_terminal()      -> if grp terminal: log + abort
#   3. compute_remaining_frames()     -> ctx.remaining
#   4. abort_if_no_remaining()        -> if remaining is None: silent abort
#   5. enforce_max_retries()          -> ctx.next_attempt; abort if > cap
#   6. load_dispatch_context()        -> ctx.file_size_bytes, engine, tier
#   7. build_retry_chunk_request()    -> ctx.chunk_request (with exclusions)
#   8. allocate_retry_task()          -> ctx.retry_task; log+abort if None
#   9. log_auto_retry()               -> "Job X: requeued frames A-B ..."
#  10. enqueue_retry_dispatch()       -> coordinator.enqueue_and_flush
# ---------------------------------------------------------------------

AUTO_RETRY_PIPELINE = (
    RetryPipelineBuilder()
    .load_job_and_group()
    .abort_if_group_terminal()
    .compute_remaining_frames()
    .abort_if_no_remaining()
    .enforce_max_retries()
    .load_dispatch_context()
    .build_retry_chunk_request()
    .allocate_retry_task()
    .log_auto_retry()
    .enqueue_retry_dispatch()
    .build()
)


# ---------------------------------------------------------------------
# MANUAL -- preserves RenderLifecycle.retry_chunk_manually exactly.
#
# Step trace (legacy lifecycle.py:351-459):
#   (not_found checks happen in lifecycle wrapper before pipeline)
#   1. manual_retry_load_group()              -> grp; raise group_cancelled
#   2. manual_retry_raise_if_active_sibling() -> raise active_sibling_exists
#   3. manual_retry_resolve_latest_sibling()  -> ctx.rj (from latest);
#                                                raise not_found / not_retryable
#   4. compute_remaining_frames()             -> ctx.remaining
#   5. manual_retry_raise_if_no_remaining()   -> raise no_remaining_frames
#   6. manual_retry_set_attempt_zero()        -> ctx.next_attempt = 0
#   7. load_dispatch_context()                -> file_size_bytes, engine, tier
#   8. build_retry_chunk_request()            -> ctx.chunk_request
#   9. manual_retry_allocate_retry_task()     -> ctx.retry_task;
#                                                raise no_eligible_target
#  10. log_manual_retry()                     -> "Manual retry for chunk ..."
#  11. mark_force_retry()                     -> ctx.force_retry = True
#                                                (bypasses terminal-group
#                                                guard for "failed" groups)
#  12. enqueue_retry_dispatch()               -> coordinator.enqueue_and_flush
#  13. manual_retry_reconcile_group()         -> reconcile_group_status
#  14. manual_retry_build_result()            -> ctx.result payload
# ---------------------------------------------------------------------

MANUAL_RETRY_PIPELINE = (
    RetryPipelineBuilder()
    .manual_retry_load_group()
    .manual_retry_raise_if_active_sibling()
    .manual_retry_resolve_latest_sibling()
    .compute_remaining_frames()
    .manual_retry_raise_if_no_remaining()
    .manual_retry_set_attempt_zero()
    .load_dispatch_context()
    .build_retry_chunk_request()
    .manual_retry_allocate_retry_task()
    .log_manual_retry()
    .mark_force_retry()
    .enqueue_retry_dispatch()
    .manual_retry_reconcile_group()
    .manual_retry_build_result()
    .build()
)
