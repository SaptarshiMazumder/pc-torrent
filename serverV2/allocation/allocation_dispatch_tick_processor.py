"""AllocationDispatchTickProcessor -- one phase of the daemon's tick.

Called by ``AllocationDispatchQueueDaemon._tick`` once per enabled
fleet.  Drains the ``dispatch_queue`` for that fleet up to its cap
and fires the fleet strategy on each popped item.

Per-iteration work:

  1. Check fleet cap (live count from JobRepository via callable).
  2. Pop the oldest queued item for this fleet.
  3. Skip the row if the group went terminal between enqueue and now.
  4. Reuse the pre-generated job_id, claim the in-progress ledger,
     resolve the blend URL, build the ``DispatchContext``, fire the
     fleet strategy via ``AllocationDispatcher``.

Does NOT mutate the snapshot.  Resource commitment already happened
upstream in ``AllocationPendingTickProcessor`` when the row was
written to dispatch_queue.  Mutating again here would double-count.

Strict fleet-name lookup: an unknown fleet here is a wiring bug, not
a runtime condition -- raises rather than silently no-op'ing.
"""

from __future__ import annotations

import logging
from typing import Callable
from uuid import uuid4

from serverV2.allocation.allocation_blend_url_resolver import (
    AllocationBlendUrlResolver,
)
from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationDispatchQueueRepository,
    AllocationQueueItem,
)
from serverV2.allocation.allocation_dispatcher import AllocationDispatcher
from serverV2.allocation.allocation_engine_resolver import (
    AllocationEngineResolver,
)
from serverV2.allocation.allocation_queue_item_codec import (
    AllocationQueueItemCodec,
)
from serverV2.core.models import DispatchContext, DispatchResult
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.services.jobs.jobs_logger.log_streamer_env_builder import (
    LogStreamerEnvBuilder,
)

log = logging.getLogger(__name__)


class AllocationDispatchTickProcessor:

    def __init__(
        self,
        *,
        queue_repo: AllocationDispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        active_count_by_fleet: Callable[[], dict[str, int]],
        dispatcher: AllocationDispatcher,
        blend_url_resolver: AllocationBlendUrlResolver,
        fleet_caps: Callable[[], dict[str, int]],
        get_group_status: Callable[[str], str | None],
        codec: AllocationQueueItemCodec,
        engine_resolver: AllocationEngineResolver,
        log_streamer_env_builder: LogStreamerEnvBuilder,
    ) -> None:
        self._queue_repo = queue_repo
        self._in_progress = in_progress_repo
        self._active_count_by_fleet = active_count_by_fleet
        self._dispatcher = dispatcher
        self._blend_url = blend_url_resolver
        self._fleet_caps = fleet_caps
        self._get_group_status = get_group_status
        self._codec = codec
        self._engine_resolver = engine_resolver
        self._log_streamer_env_builder = log_streamer_env_builder

    def process(self, fleet: str) -> int:
        """Drain ``dispatch_queue`` for one fleet until cap or empty.
        Returns the number of items actually dispatched."""
        caps = self._fleet_caps()
        if fleet not in caps:
            raise KeyError(
                f"AllocationDispatchTickProcessor: no cap configured for "
                f"fleet={fleet!r}.  Known fleets: "
                f"{sorted(caps.keys())}"
            )
        cap = caps[fleet]
        dispatched = 0
        popped_count = 0
        while True:
            current = self._active_count_by_fleet().get(fleet, 0)
            if current >= cap:
                if popped_count > 0:
                    log.info(
                        "[RETRY_DEBUG] dispatch_tick(%s): cap reached (%d/%d) after popping %d",
                        fleet, current, cap, popped_count,
                    )
                break
            item = self._queue_repo.dequeue_for_fleet(fleet)
            if item is None:
                if popped_count > 0:
                    log.info(
                        "[RETRY_DEBUG] dispatch_tick(%s): queue empty after %d items",
                        fleet, popped_count,
                    )
                break
            popped_count += 1
            log.info(
                "[RETRY_DEBUG] dispatch_tick(%s): popped queue item job_id=%s "
                "group=%s chunk=%s attempt=%d",
                fleet, item.job_id, item.group_id, item.chunk_index, item.attempt,
            )
            # Terminal-group guard.  Drop the row if the parent group
            # is in any non-active state.  Manual retry's responsibility
            # is to flip the group OUT of terminal before enqueueing
            # (``ManualRetryFlipGroupPendingStep``); auto-retry from a
            # failed/cancelled/done group has nothing to retry into.
            if item.group_id:
                grp_status = self._get_group_status(item.group_id)
                if grp_status is None:
                    log.info(
                        "dispatch_tick(%s): dropping queued chunk %s -- "
                        "group %s no longer exists (job_id=%s)",
                        fleet, item.chunk_index, item.group_id, item.job_id,
                    )
                    continue
                if grp_status not in ("pending", "running"):
                    log.info(
                        "dispatch_tick(%s): dropping queued chunk %s -- "
                        "group %s is %s (job_id=%s)",
                        fleet, item.chunk_index, item.group_id, grp_status, item.job_id,
                    )
                    continue
            result = self._dispatch_one(item)
            if result is not None:
                dispatched += 1
                log.info(
                    "[RETRY_DEBUG] dispatch_tick(%s): dispatched job_id=%s status=%s",
                    fleet, item.job_id, result.status,
                )
            else:
                log.warning(
                    "[RETRY_DEBUG] dispatch_tick(%s): _dispatch_one returned None "
                    "for job_id=%s",
                    fleet, item.job_id,
                )
        if dispatched:
            log.info(
                "dispatch_tick(%s): dispatched %d queued chunk(s)",
                fleet, dispatched,
            )
        return dispatched

    def has_any_for_fleets(self, fleets: list[str]) -> bool:
        """Idle-tick guard: returns True iff at least one row is queued
        for any of the given fleets.  Lets the daemon skip the
        snapshot fetch + end-of-tick persist when there's nothing to
        dispatch."""
        return self._queue_repo.has_any_for_fleets(fleets)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _dispatch_one(
        self,
        item: AllocationQueueItem,
    ) -> DispatchResult | None:
        group_id = item.group_id
        if not group_id:
            log.warning(
                "Queue item missing group_id (chunk %s) -- dropping",
                item.chunk_index,
            )
            return None

        task = self._codec.decode(item)
        blend_url = self._blend_url.resolve(task.fleet, group_id, item.input_filename)

        # job_id was pre-generated at enqueue time and stored on the
        # queue row.  Reuse it so the upstream caller's recorded job_id
        # matches the actual dispatched job.  Claim the in-progress
        # ledger BEFORE dispatch so a synchronous failure can still be
        # deduplicated by the retry chain's stale-signal guard.
        job_id = item.job_id or str(uuid4())

        # jobs_logger: stamp the PCR_* env dict onto the context so
        # every fleet strategy (current + future) forwards a signed
        # log-append URL + identity headers into the worker container.
        # Single call site -- a new fleet cannot skip log capture.
        log_streamer_env = self._log_streamer_env_builder.build(
            job_id=job_id,
            group_id=group_id,
            attempt=task.attempt,
            chunk_index=task.chunk_index or 0,
            fleet=task.fleet,
            machine_id=task.machine_id or "",
        )
        dispatch_ctx = DispatchContext(
            group_id=group_id,
            input_filename=item.input_filename,
            render_overrides_json=item.render_overrides_json,
            blend_url=blend_url,
            log_streamer_env=log_streamer_env,
            max_retries=item.max_retries,
            priority=item.priority,
            engine=self._engine_resolver.from_overrides_json(item.render_overrides_json),
        )
        self._in_progress.claim_or_replace(
            group_id=group_id,
            chunk_index=task.chunk_index or 0,
            job_id=job_id,
            attempt=task.attempt,
        )

        try:
            return self._dispatcher.dispatch_one(task, dispatch_ctx, job_id=job_id)
        except Exception as exc:
            log.error(
                "Dispatch failed for frames %d-%d (group %s): %s",
                task.frame_start, task.frame_end, group_id, exc,
            )
            return None
