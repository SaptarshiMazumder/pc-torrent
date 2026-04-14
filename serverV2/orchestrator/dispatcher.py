"""Dispatcher — sends PlannedTasks to the correct fleet strategy.

Handles threading differences between providers (Modal: parallel with
stagger, Vast: sequential with sleep).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from serverV2.core.models import DispatchContext, DispatchResult, PlannedTask
from serverV2.fleets.registry import FleetRegistry

log = logging.getLogger(__name__)


class Dispatcher:

    def __init__(self, registry: FleetRegistry) -> None:
        self._registry = registry

    def dispatch_all(
        self,
        tasks: list[PlannedTask],
        context: DispatchContext,
    ) -> list[DispatchResult]:
        results: list[DispatchResult] = []
        tasks_by_type: dict[str, list[PlannedTask]] = {}

        for task in tasks:
            tasks_by_type.setdefault(task.machine_type, []).append(task)

        for machine_type, typed_tasks in tasks_by_type.items():
            strategy = self._registry.get(machine_type)
            if not strategy or not strategy.is_enabled():
                continue

            if machine_type == "modal_serverless":
                results.extend(self._dispatch_modal(typed_tasks, context))
            elif machine_type == "vast_serverless":
                results.extend(self._dispatch_vast(typed_tasks, context))
            else:
                for task in typed_tasks:
                    results.append(strategy.dispatch(task, context))

        return results

    def dispatch_one(self, task: PlannedTask, context: DispatchContext) -> DispatchResult:
        strategy = self._registry.get_or_raise(task.machine_type)
        return strategy.dispatch(task, context)

    def _dispatch_modal(
        self, tasks: list[PlannedTask], context: DispatchContext,
    ) -> list[DispatchResult]:
        results: list[DispatchResult] = []
        strategy = self._registry.get_or_raise("modal_serverless")
        for i, task in enumerate(tasks):
            if i > 0:
                time.sleep(0.05)

            def _do(t: PlannedTask = task) -> None:
                try:
                    result = strategy.dispatch(t, context)
                    results.append(result)
                except Exception as exc:
                    log.error("Modal dispatch failed: %s", exc)

            thread = threading.Thread(target=_do, daemon=True, name=f"modal-dispatch-{i}")
            thread.start()
            thread.join(timeout=300)
        return results

    def _dispatch_vast(
        self, tasks: list[PlannedTask], context: DispatchContext,
    ) -> list[DispatchResult]:
        results: list[DispatchResult] = []
        strategy = self._registry.get_or_raise("vast_serverless")

        def _sequential() -> None:
            for task in tasks:
                try:
                    result = strategy.dispatch(task, context)
                    results.append(result)
                except Exception as exc:
                    log.error("Vast dispatch failed: %s", exc)
                time.sleep(2)

        thread = threading.Thread(target=_sequential, daemon=True, name="vast-dispatch")
        thread.start()
        thread.join(timeout=600)
        return results
