"""MonitorLockSweeper — fleet-agnostic thread driver.

Runs on every Cloud Run instance.  Each tick (~30s) iterates the
injected list of MonitorLockSweepStrategy implementations and calls
``sweep()`` on each.  The strategies own the per-fleet logic; this
class owns nothing but the thread lifecycle.

A strategy that raises is logged and skipped -- one fleet's failure
must not stall the others.

Sweep latency bounds for an orphaned monitor (previous owner died):
  * Best:   sweep_interval (~30s) after the lock TTL expires
  * Worst:  sweep_interval + lock_TTL (~90s)
The backup_monitor cron catches anything still orphaned beyond that
via the heartbeat-stale path.
"""

from __future__ import annotations

import logging
import threading

from serverV2.monitor_lock.monitor_lock_service.monitor_lock_sweep_strategy import (
    MonitorLockSweepStrategy,
)

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SEC = 30


class MonitorLockSweeper:

    def __init__(
        self,
        *,
        strategies: list[MonitorLockSweepStrategy],
        interval_sec: int = _DEFAULT_INTERVAL_SEC,
    ) -> None:
        self._strategies = strategies
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="monitor-lock-sweeper",
        )
        self._thread.start()
        log.info(
            "MonitorLockSweeper started (interval=%ds, strategies=%d)",
            self._interval, len(self._strategies),
        )

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for strategy in self._strategies:
                try:
                    strategy.sweep()
                except Exception:
                    log.exception(
                        "MonitorLockSweep error in %s",
                        type(strategy).__name__,
                    )
            self._stop.wait(self._interval)
