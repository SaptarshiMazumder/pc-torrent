"""Orchestrator-side read-only repositories.

Allocation owns every WRITE on the queue tables (dispatch_queue,
pending_allocation_queue) — those go through ``AllocationFacade``.

When orchestration needs to READ from one of those tables to drive
its own logic (group-status aggregation, status DTOs, etc.), the read
lives here.  This keeps the SQL grouped by consumer-context: when the
aggregator's needs change, we change the read here without touching
allocation's command surface.

Allocation never imports anything from this package; the dependency
points one way (orchestrator → allocation tables, read-only).
"""

from serverV2.orchestrator.repositories.pending_allocation_repository import (
    PendingAllocationRepository,
)

__all__ = ["PendingAllocationRepository"]
