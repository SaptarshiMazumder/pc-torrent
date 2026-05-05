"""Pre-built retry pipelines.

Two immutable tuples constructed once at import time:

* ``AUTO_RETRY_PIPELINE`` -- silent short-circuit on abort (group
  terminal / no remaining frames / max retries).  On success: parks
  the retry chunk on ``pending_allocation_queue`` for the dispatch
  daemon to plan + dispatch on its next tick.

* ``MANUAL_RETRY_PIPELINE`` -- raise-on-refusal flow (group_cancelled,
  active_sibling_exists, not_retryable, no_remaining_frames).  On
  success: flips the group status back to ``pending`` and parks the
  retry chunk.  Endpoint returns immediately with ``queued_for_retry``;
  the daemon picks a target asynchronously.

Why both flows park instead of plan synchronously:

The dispatch daemon is the SOLE owner of the fleet-availability
snapshot's mutable state.  Within a tick, it reads the cached snapshot
once, mutates a local view as it dispatches each queued item, and
persists the mutated view at end-of-tick.  If a retry callback (auto)
or HTTP request thread (manual) reads the cache and plans
synchronously, it races with the daemon's mid-tick mutations -- the
cache only persists at end-of-tick, so out-of-tick readers see
pre-tick state and can pick a target the daemon has already consumed.
Result: dogpile (multiple chunks dispatched to the same machine).

By parking, the only thread reading or writing fleet availability is
the daemon.  No race possible.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.retry_pipeline_builder import (
    RetryPipelineBuilder,
)


# ---------------------------------------------------------------------
# AUTO -- silent abort steps + park-to-pending.
#
# Step trace:
#   1. load_job_and_group()           -> ctx.rj, group_id, chunk_index, grp
#   2. abort_if_group_terminal()      -> if grp terminal: log + abort
#   3. compute_remaining_frames()     -> ctx.remaining
#   4. abort_if_no_remaining()        -> if remaining is None: silent abort
#   5. enforce_max_retries()          -> ctx.next_attempt; abort if > cap
#   6. load_dispatch_context()        -> ctx.engine, ctx.tier
#   7. build_retry_chunk_request()    -> ctx.chunk_request (with exclusions)
#   8. park_retry_to_pending()        -> writes pending_allocation_queue row,
#                                        sets ctx.parked = True
#   9. log_auto_retry()               -> "Job X: parked retry frames A-B ..."
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
    .park_retry_to_pending()
    .log_auto_retry()
    .build()
)


# ---------------------------------------------------------------------
# MANUAL -- raise-on-refusal flow.  Same parking shape as auto for the
# same race reason.  ``no_eligible_target`` is no longer a synchronous
# refusal: if no fleet has capacity, the parked row stays parked across
# daemon ticks until something becomes available.  The endpoint returns
# ``queued_for_retry`` immediately and the UI polls the group detail to
# observe the chunk getting dispatched.
#
# Step trace:
#   (not_found checks happen in lifecycle wrapper before pipeline)
#   1. manual_retry_load_group()              -> grp; raise group_cancelled
#   2. manual_retry_raise_if_active_sibling() -> raise active_sibling_exists
#   3. manual_retry_resolve_latest_sibling()  -> ctx.rj (from latest);
#                                                raise not_found / not_retryable
#   4. compute_remaining_frames()             -> ctx.remaining
#   5. manual_retry_raise_if_no_remaining()   -> raise no_remaining_frames
#   6. manual_retry_set_attempt_zero()        -> ctx.next_attempt = 0
#   7. load_dispatch_context()                -> engine, tier
#   8. build_retry_chunk_request()            -> ctx.chunk_request
#   9. manual_retry_flip_group_pending()      -> if grp terminal:
#                                                  group_repo.update_status('pending')
#                                                (must run BEFORE park so
#                                                 the daemon's read sees the
#                                                 active status)
#  10. park_retry_to_pending()                -> pending_allocation_queue row
#  11. log_manual_retry()                     -> "Manual retry parked ..."
#  12. manual_retry_build_result()            -> ctx.result payload
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
    .manual_retry_flip_group_pending()
    .park_retry_to_pending()
    .log_manual_retry()
    .manual_retry_build_result()
    .build()
)
