"""Allocation module — frame-range planning + dispatch queue + dispatch.

Public surface (only thing orchestrator imports):
  * ``AllocationFacade``                 — entry, 3 thin delegations
  * ``AllocationDispatchQueueDaemon``    — background tick (started by bootstrap)

Internals (services, strategies, repositories, dispatcher) are not
exported.  Orchestrator never reaches past the facade.
"""

from serverV2.allocation.allocation_dispatch_queue_daemon import (
    AllocationDispatchQueueDaemon,
)
from serverV2.allocation.allocation_facade import AllocationFacade

__all__ = [
    "AllocationDispatchQueueDaemon",
    "AllocationFacade",
]
