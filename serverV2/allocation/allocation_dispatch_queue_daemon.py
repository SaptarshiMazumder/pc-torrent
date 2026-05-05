"""AllocationDispatchQueueDaemon -- the SOLE orchestrator of allocation ticks.

Every Cloud Run replica calls ``start()`` at boot.  The thread runs on
every replica; the Redis singleton lock at ``allocation:dispatch:daemon``
ensures only one replica's tick actually fires per interval.  Pattern
mirrors ``CommunityMonitor`` / ``MonitorLockRepository``.

Per tick (byte-for-byte equivalent to the previous implementation,
just with the orchestration moved up here from a service wrapper):

  1. Idle-tick guard: skip if both queues are empty.
  2. ``snapshot = snapshot_cache.get_or_build()``  -- Redis read.
  3. ``mutable = snapshot.to_mutable()``           -- in-memory view.
  4. For each enabled fleet, drain the dispatch_queue via
     ``AllocationDispatchTickProcessor.process(fleet)``.  Each item
     popped fires its fleet strategy and writes a ``jobs`` row.  No
     mutation here -- commitment already happened upstream.
  5. ``AllocationPendingTickProcessor.process(mutable)`` walks the
     pending_queue, plans each row, writes resulting tasks to the
     dispatch_queue, and mutates ``mutable`` per pick (preventing
     intra-tick dogpile).
  6. ``snapshot_cache.persist(mutable.to_frozen())`` -- Redis write
     with ``KEEPTTL XX`` (preserves natural expiry; no-op if cache
     expired between read and write).

Helpers do their work; the daemon is the sole place orchestration
order is encoded.
"""

from __future__ import annotations

import logging
import threading

from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationDispatchQueueRepository,
)
from serverV2.allocation.allocation_dispatch_tick_processor import (
    AllocationDispatchTickProcessor,
)
from serverV2.allocation.allocation_pending_queue_repository import (
    AllocationPendingQueueRepository,
)
from serverV2.allocation.allocation_pending_tick_processor import (
    AllocationPendingTickProcessor,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot_cache import (
    FleetAvailabilitySnapshotCache,
)
from serverV2.monitor_lock.monitor_lock_repository import MonitorLockRepository

log = logging.getLogger(__name__)

_LOCK_KEY = "allocation:dispatch:daemon"


class AllocationDispatchQueueDaemon:

    DEFAULT_TICK_INTERVAL_S = 2.0

    def __init__(
        self,
        *,
        dispatch_repo: AllocationDispatchQueueRepository,
        pending_repo: AllocationPendingQueueRepository,
        dispatch_tick_processor: AllocationDispatchTickProcessor,
        pending_tick_processor: AllocationPendingTickProcessor,
        snapshot_cache: FleetAvailabilitySnapshotCache,
        lock_repo: MonitorLockRepository,
        instance_id: str,
        enabled_fleets: list[str],
        tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
    ) -> None:
        self._dispatch_repo = dispatch_repo
        self._pending_repo = pending_repo
        self._dispatch_tick = dispatch_tick_processor
        self._pending_tick = pending_tick_processor
        self._snapshot_cache = snapshot_cache
        self._lock_repo = lock_repo
        self._instance_id = instance_id
        self._enabled_fleets = list(enabled_fleets)
        self._tick_interval_s = tick_interval_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._owns_lock = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="AllocationDispatchQueueDaemon",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "AllocationDispatchQueueDaemon started (interval=%.1fs, fleets=%s)",
            self._tick_interval_s, self._enabled_fleets,
        )

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None
        if self._owns_lock:
            self._lock_repo.release(_LOCK_KEY, self._instance_id)
            self._owns_lock = False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if not self._gate_lock():
                    # Another replica owns the daemon -- sleep and retry.
                    self._stop_event.wait(self._tick_interval_s)
                    continue
                self._tick()
            except Exception as exc:
                log.error("AllocationDispatchQueueDaemon tick raised: %s", exc)
            self._stop_event.wait(self._tick_interval_s)

    def _gate_lock(self) -> bool:
        """Maintain singleton ownership.  Returns True iff we own the
        lock for the upcoming tick."""
        if self._owns_lock:
            if self._lock_repo.refresh(_LOCK_KEY, self._instance_id):
                return True
            # Lost ownership -- Redis hiccup or clock skew.  Drop the
            # flag and try to reacquire next iteration.
            log.info(
                "AllocationDispatchQueueDaemon lost lock -- yielding to "
                "another replica",
            )
            self._owns_lock = False
            return False
        if self._lock_repo.try_acquire(_LOCK_KEY, self._instance_id):
            self._owns_lock = True
            log.info("AllocationDispatchQueueDaemon acquired singleton lock")
            return True
        return False

    def _tick(self) -> None:
        # Idle-tick guard: if BOTH queues are empty, skip the snapshot
        # fetch + end-of-tick persist entirely.  Saves a Redis GET +
        # SET KEEPTTL on every idle tick, and (on cache expiry every
        # 60s) saves the parallel rebuild that hits Vast HTTPS, Modal
        # SCARDs, the community DB query, and the JobRepository COUNT.
        has_dispatch = self._dispatch_tick.has_any_for_fleets(self._enabled_fleets)
        has_pending = self._pending_tick.has_any()
        if not (has_dispatch or has_pending):
            return

        # --- Redis read -------------------------------------------------
        snapshot = self._snapshot_cache.get_or_build()
        mutable = snapshot.to_mutable()

        # --- Phase 1: drain dispatch_queue per fleet -------------------
        for fleet in self._enabled_fleets:
            try:
                self._dispatch_tick.process(fleet)
            except Exception as exc:
                log.warning(
                    "dispatch tick (%s) failed: %s",
                    fleet, exc,
                )

        # --- Phase 2: plan pending_queue rows -> dispatch_queue --------
        # The planner mutates ``mutable`` for every committed task
        # before the next row is planned, so two pending rows never
        # race on the same target.
        try:
            self._pending_tick.process(mutable)
        except Exception as exc:
            log.warning("pending tick failed: %s", exc)

        # --- Redis write (KEEPTTL XX -- preserves natural expiry) ------
        self._snapshot_cache.persist(mutable.to_frozen())
