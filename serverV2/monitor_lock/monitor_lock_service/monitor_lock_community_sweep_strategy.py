"""MonitorLockCommunitySweepStrategy — claim the community singleton lock.

Per tick, call ``try_start()`` on the supervised singleton.  Same shape
as the Vast and Modal sweep strategies now that all three fleet
monitors are singletons.

Constructor takes the Protocol from this module (``MonitorLockDaemon``)
rather than the concrete fleet-monitor class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serverV2.monitor_lock.monitor_lock_daemon import MonitorLockDaemon


class MonitorLockCommunitySweepStrategy:

    def __init__(self, *, community_monitor: "MonitorLockDaemon") -> None:
        self._community_monitor = community_monitor

    def sweep(self) -> None:
        self._community_monitor.try_start()
