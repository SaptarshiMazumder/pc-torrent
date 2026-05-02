"""monitor_lock — singleton-monitor coordination across Cloud Run instances.

Every long-running monitor (per-job Vast + Modal, singleton Community)
runs on at most one instance at a time, gated by a Redis lock.  When
the owning instance dies, the lock TTL expires and a sweeper running on
every instance re-claims the orphaned monitor on the next tick.

Public surface (this ``__init__``):

* ``MonitorLockFacade``     — start / stop the sweep daemon (one per process)
* ``MonitorLockRepository`` — Redis lock primitive used directly by the
                              monitor managers and the sweep strategies

The service layer (sweeper, sweep-strategy implementations) is internal.
Callers never reach past the facade or repository.
"""

from serverV2.monitor_lock.monitor_lock_facade import MonitorLockFacade
from serverV2.monitor_lock.monitor_lock_repository import MonitorLockRepository

__all__ = ["MonitorLockFacade", "MonitorLockRepository"]
