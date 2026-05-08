"""MonitorLockDaemon — contract for any singleton thread the sweep
daemon supervises.

Implementations live in their owning fleet (``VastFleetMonitor``,
``ModalFleetMonitor``, ``CommunityMonitor``) and own their own Redis
lock acquire / refresh / release internally via ``MonitorLockRepository``.
The sweep daemon pokes ``try_start()`` on each registered conformer
every ~30 s so failover after a holder dies is automatic: the lock
TTL expires, the next sweep tick on any other instance reclaims it.

Structural typing — implementations do **not** inherit from this
Protocol.  Any class with ``try_start() -> bool`` conforms; the
``@runtime_checkable`` decorator lets ``isinstance(x, MonitorLockDaemon)``
work too.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MonitorLockDaemon(Protocol):

    def try_start(self) -> bool:
        """Attempt to claim the singleton lock and start the daemon
        thread on this Cloud Run instance.

        Idempotent within a process: a second call while we already
        own the lock and the thread is alive is a no-op (returns True).

        Returns True iff this instance now owns the singleton (either
        just claimed it, or already did).  False iff another instance
        currently holds the lock.
        """
        ...
