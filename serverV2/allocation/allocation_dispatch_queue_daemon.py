"""AllocationDispatchQueueDaemon — background tick driver.

Every Cloud Run replica calls ``start()`` at boot.  The thread runs on
every replica; the Redis singleton lock at ``allocation:dispatch:daemon``
ensures only one replica's tick actually fires per interval.  Pattern
mirrors ``CommunityMonitor`` / ``MonitorLockRepository``.

Per tick:
  1. Read fleet availability from the cache (one Redis hit, or a
     parallel rebuild on cache miss).
  2. Hand the mutable view to ``AllocationDispatchQueueService``,
     which runs ``dispatch_pending_for_fleet(f)`` for each enabled
     fleet.  The service mutates the snapshot after each successful
     dispatch.
  3. Persist the mutated snapshot back to the cache (KEEPTTL +
     XX-only — never extends the freshness window).
"""

from __future__ import annotations

import logging
import threading

from serverV2.allocation.services.allocation_dispatch_queue_service import (
    AllocationDispatchQueueService,
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
        dispatch_queue: AllocationDispatchQueueService,
        snapshot_cache: FleetAvailabilitySnapshotCache,
        lock_repo: MonitorLockRepository,
        instance_id: str,
        enabled_fleets: list[str],
        tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
    ) -> None:
        self._dispatch_queue = dispatch_queue
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
                    # Another replica owns the daemon — sleep and retry.
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
            # Lost ownership — Redis hiccup or clock skew.  Drop the
            # flag and try to reacquire next iteration.
            log.info(
                "AllocationDispatchQueueDaemon lost lock — yielding to "
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
        # Idle-tick guard: if nothing is queued for any enabled fleet,
        # skip the snapshot fetch and end-of-tick persist entirely.
        # Saves a Redis GET + SET KEEPTTL on every idle tick, and (on
        # cache expiry every 60s) saves the parallel rebuild that hits
        # Vast HTTPS, Modal SCARDs, the community DB query, and the
        # JobRepository COUNT.  One LIMIT 1 SELECT against an indexed
        # column is the only DB cost on idle ticks.
        if not self._dispatch_queue.has_any_for_fleets(self._enabled_fleets):
            return

        snapshot = self._snapshot_cache.get_or_build()
        mutable = snapshot.to_mutable()
        for fleet in self._enabled_fleets:
            try:
                self._dispatch_queue.dispatch_pending_for_fleet(fleet, mutable)
            except Exception as exc:
                log.warning(
                    "dispatch_pending_for_fleet(%s) failed in tick: %s",
                    fleet, exc,
                )
        self._snapshot_cache.persist(mutable.to_frozen())
