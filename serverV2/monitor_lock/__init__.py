"""monitor_lock — sweep-daemon supervision and Redis lock primitive.

Every long-running fleet singleton (``VastFleetMonitor``,
``ModalFleetMonitor``, ``CommunityMonitor``) runs on at most one Cloud
Run instance at a time, gated by a Redis lock.  When the owning
instance dies, the lock TTL expires and the sweep daemon running on
every other instance reclaims the orphaned singleton on the next tick.

Public surface (this ``__init__``):

* ``MonitorLockFacade``     — start / stop the sweep daemon (one per process)
* ``MonitorLockRepository`` — Redis lock primitive used directly by the
                              fleet singletons themselves
* ``MonitorLockDaemon``     — Protocol that fleet singletons structurally
                              implement so this module owns the contract
                              for "what the sweep daemon supervises"

Internal: the sweeper, the sweep-strategy implementations.  Callers
never reach past the facade or repository.
"""

from serverV2.monitor_lock.monitor_lock_daemon import MonitorLockDaemon
from serverV2.monitor_lock.monitor_lock_facade import MonitorLockFacade
from serverV2.monitor_lock.monitor_lock_repository import MonitorLockRepository

__all__ = ["MonitorLockDaemon", "MonitorLockFacade", "MonitorLockRepository"]
