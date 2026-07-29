"""Adapter: implements rendering's ``IAllocationService`` by delegating to
the allocation concern (planner + pending/dispatch queues).

Rendering asks only two things of allocation -- submit a confirmed group,
and report whether work is still in flight for the reconcile guard -- so this
adapter stays a thin translation over the allocation facade.
"""

from __future__ import annotations

from typing import Any

from serverV2.rendering.render_group.application.ports.allocation_service import IAllocationService


class AllocationServiceAdapter(IAllocationService):
    def __init__(self, allocation: Any) -> None:
        self._allocation = allocation

    def submit(self, group_id: str) -> None:
        raise NotImplementedError

    def has_work_in_flight(self, group_id: str) -> bool:
        raise NotImplementedError
