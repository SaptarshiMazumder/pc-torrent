"""MonitorLockVastSweepStrategy — claim the Vast singleton lock.

Per tick, call ``try_start()`` on the supervised singleton.  The lock
acquire inside that method decides whether THIS Cloud Run instance
now runs the Vast fleet scan.

Constructor takes the Protocol from this module (``MonitorLockDaemon``)
rather than the concrete fleet-monitor class -- monitor_lock owns the
contract for what counts as a supervised singleton.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serverV2.monitor_lock.monitor_lock_daemon import MonitorLockDaemon


class MonitorLockVastSweepStrategy:

    def __init__(self, *, vast_fleet_monitor: "MonitorLockDaemon") -> None:
        self._fleet_monitor = vast_fleet_monitor

    def sweep(self) -> None:
        self._fleet_monitor.try_start()
