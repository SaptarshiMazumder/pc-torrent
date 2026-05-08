"""MonitorLockModalSweepStrategy — claim the Modal singleton lock.

Per tick, call ``try_start()`` on the supervised singleton.  Same
shape as the Vast and community sweep strategies.

Constructor takes the Protocol from this module (``MonitorLockDaemon``)
rather than the concrete fleet-monitor class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serverV2.monitor_lock.monitor_lock_daemon import MonitorLockDaemon


class MonitorLockModalSweepStrategy:

    def __init__(self, *, modal_fleet_monitor: "MonitorLockDaemon") -> None:
        self._fleet_monitor = modal_fleet_monitor

    def sweep(self) -> None:
        self._fleet_monitor.try_start()
