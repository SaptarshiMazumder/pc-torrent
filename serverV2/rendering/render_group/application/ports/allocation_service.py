"""Port: what rendering needs from the ALLOCATION concern.

Allocation is a peer concern (also driven by the dispatch daemon), so
rendering reaches it through this narrow abstraction rather than importing
the allocation planner directly.

Two needs only:
  * ``submit`` -- hand a confirmed group to allocation to be planned and
    dispatched into chunks.
  * ``has_work_in_flight`` -- the reconcile guard: a group must not flip
    terminal while a retry still sits on the pending/dispatch queues.
"""

from __future__ import annotations

from typing import Protocol


class IAllocationService(Protocol):
    def submit(self, group_id: str) -> None: ...

    def has_work_in_flight(self, group_id: str) -> bool: ...
