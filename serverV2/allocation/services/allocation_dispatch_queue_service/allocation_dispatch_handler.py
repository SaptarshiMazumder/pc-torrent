"""AllocationDispatchHandler — read side of the dispatch queue.

One responsibility: the cap-gated pop+dispatch loop the daemon calls
once per fleet per tick.  Per-iteration work:

  1. Check fleet cap (live count from JobRepository via callable).
  2. Pop the oldest queued item for this fleet.
  3. Skip the row if the group went terminal between enqueue and now.
  4. Reuse the pre-generated job_id, claim the in-progress ledger,
     resolve the blend URL, build the ``DispatchContext``, fire the
     fleet strategy.
  5. On success, mutate the in-tick snapshot via
     ``AllocationSnapshotMutator`` so the daemon's end-of-tick cache
     write reflects post-dispatch state.

Strict fleet-name lookup: an unknown fleet here is a wiring bug, not
a runtime condition — raises rather than silently no-op'ing.
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
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_engine_resolver import (
    AllocationEngineResolver,
)
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_queue_item_codec import (
    AllocationQueueItemCodec,
)
from serverV2.allocation.services.allocation_dispatch_queue_service.allocation_snapshot_mutator import (
    AllocationSnapshotMutator,
)
from serverV2.core.models import DispatchContext, DispatchResult
from serverV2.fleets.fleet_availability.mutable_fleet_availability_snapshot import (
    MutableFleetAvailabilitySnapshot,
)
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository

log = logging.getLogger(__name__)


class AllocationDispatchHandler:

    def __init__(
        self,
        *,
        queue_repo: AllocationDispatchQueueRepository,
        in_progress_repo: InProgressChunkRepository,
        active_count_by_fleet: Callable[[], dict[str, int]],
        dispatcher: AllocationDispatcher,
        blend_url_resolver: AllocationBlendUrlResolver,
        fleet_caps: dict[str, int],
        get_group_status: Callable[[str], str | None],
        codec: AllocationQueueItemCodec,
        engine_resolver: AllocationEngineResolver,
        snapshot_mutator: AllocationSnapshotMutator,
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
        self._snapshot_mutator = snapshot_mutator

    def has_any_for_fleets(self, fleets: list[str]) -> bool:
        """Cheap existence check used by the daemon's pre-tick guard.
        Returns True iff at least one row is queued for any of the
        given fleets.  Lets the daemon skip the snapshot fetch + the
        end-of-tick persist when there's nothing to dispatch.
        """
        return self._queue_repo.has_any_for_fleets(fleets)

    def dispatch_pending_for_fleet(
        self,
        fleet: str,
        snapshot: MutableFleetAvailabilitySnapshot | None = None,
    ) -> int:
        if fleet not in self._fleet_caps:
            raise KeyError(
                f"AllocationDispatchHandler: no cap configured for fleet={fleet!r}. "
                f"Known fleets: {sorted(self._fleet_caps.keys())}"
            )
        cap = self._fleet_caps[fleet]
        dispatched = 0
        popped_count = 0
        while True:
            current = self._active_count_by_fleet().get(fleet, 0)
            if current >= cap:
                if popped_count > 0:
                    log.info(
                        "[RETRY_DEBUG] dispatch_pending_for_fleet(%s): cap reached (%d/%d) after popping %d",
                        fleet, current, cap, popped_count,
                    )
                break
            item = self._queue_repo.dequeue_for_fleet(fleet)
            if item is None:
                if popped_count > 0:
                    log.info(
                        "[RETRY_DEBUG] dispatch_pending_for_fleet(%s): queue empty after %d items",
                        fleet, popped_count,
                    )
                break
            popped_count += 1
            log.info(
                "[RETRY_DEBUG] dispatch_pending_for_fleet(%s): popped queue item job_id=%s "
                "group=%s chunk=%s attempt=%d",
                fleet, item.job_id, item.group_id, item.chunk_index, item.attempt,
            )
            # Terminal-group guard.  Policy:
            #   * cancelled / done       → always drop (group is closed)
            #   * group missing          → drop (orphan)
            #   * failed + force_retry   → dispatch (manual retry override)
            #   * failed + !force_retry  → drop (auto-retry can't resurrect a failed group)
            #   * any other status       → dispatch (normal case)
            if item.group_id:
                grp_status = self._get_group_status(item.group_id)
                if grp_status in ("cancelled", "done"):
                    log.info(
                        "dispatch_pending_for_fleet(%s): dropping queued chunk %s — "
                        "group %s is %s (job_id=%s)",
                        fleet, item.chunk_index, item.group_id, grp_status, item.job_id,
                    )
                    continue
                if grp_status is None:
                    log.info(
                        "dispatch_pending_for_fleet(%s): dropping queued chunk %s — "
                        "group %s no longer exists (job_id=%s)",
                        fleet, item.chunk_index, item.group_id, item.job_id,
                    )
                    continue
                if grp_status == "failed" and not item.force_retry:
                    log.info(
                        "dispatch_pending_for_fleet(%s): dropping queued chunk %s — "
                        "group %s is failed and row is not force_retry (job_id=%s)",
                        fleet, item.chunk_index, item.group_id, item.job_id,
                    )
                    continue
            result = self._dispatch_one(item)
            if result is not None:
                dispatched += 1
                log.info(
                    "[RETRY_DEBUG] dispatch_pending_for_fleet(%s): dispatched job_id=%s status=%s",
                    fleet, item.job_id, result.status,
                )
                if snapshot is not None:
                    self._snapshot_mutator.mark_dispatched(snapshot, item)
            else:
                log.warning(
                    "[RETRY_DEBUG] dispatch_pending_for_fleet(%s): _dispatch_one returned None "
                    "for job_id=%s",
                    fleet, item.job_id,
                )
        if dispatched:
            log.info(
                "dispatch_pending_for_fleet(%s): dispatched %d queued chunk(s)",
                fleet, dispatched,
            )
        return dispatched

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
                "Queue item missing group_id (chunk %s) — dropping",
                item.chunk_index,
            )
            return None

        task = self._codec.decode(item)
        blend_url = self._blend_url.resolve(task.fleet, group_id, item.input_filename)
        dispatch_ctx = DispatchContext(
            group_id=group_id,
            input_filename=item.input_filename,
            render_overrides_json=item.render_overrides_json,
            blend_url=blend_url,
            max_retries=item.max_retries,
            priority=item.priority,
            engine=self._engine_resolver.from_overrides_json(item.render_overrides_json),
        )

        # job_id was pre-generated at enqueue time and stored on the
        # queue row.  Reuse it so the upstream caller's recorded job_id
        # matches the actual dispatched job.  Claim the in-progress
        # ledger BEFORE dispatch so a synchronous failure can still be
        # deduplicated by the retry chain's stale-signal guard.
        job_id = item.job_id or str(uuid4())
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
