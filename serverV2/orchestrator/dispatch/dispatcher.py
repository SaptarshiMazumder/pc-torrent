"""Dispatcher — routes a PlannedTask to the correct fleet strategy."""

from __future__ import annotations

import logging

from serverV2.core.models import DispatchContext, DispatchResult, PlannedTask
from serverV2.fleets.registry import FleetRegistry

log = logging.getLogger(__name__)


class Dispatcher:

    def __init__(self, registry: FleetRegistry) -> None:
        self._registry = registry

    def dispatch_one(
        self,
        task: PlannedTask,
        context: DispatchContext,
        job_id: str,
    ) -> DispatchResult:
        strategy = self._registry.get_or_raise(task.fleet)
        return strategy.dispatch(task, context, job_id=job_id)
