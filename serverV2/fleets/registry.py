"""FleetRegistry — Factory that maps machine_type -> IFleetStrategy.

Strategies are registered at boot time.  Lookup is O(1).
"""

from __future__ import annotations

import logging

from serverV2.core.interfaces import IFleetStrategy

log = logging.getLogger(__name__)


class FleetRegistry:

    def __init__(self) -> None:
        self._strategies: dict[str, IFleetStrategy] = {}

    def register(self, strategy: IFleetStrategy) -> None:
        self._strategies[strategy.machine_type] = strategy
        log.info("Registered fleet strategy: %s", strategy.machine_type)

    def get(self, machine_type: str) -> IFleetStrategy | None:
        return self._strategies.get(machine_type)

    def get_or_raise(self, machine_type: str) -> IFleetStrategy:
        strategy = self.get(machine_type)
        if strategy is None:
            raise KeyError(f"No fleet strategy for machine_type={machine_type!r}")
        return strategy

    def all_strategies(self) -> list[IFleetStrategy]:
        return list(self._strategies.values())

    def enabled_types(self) -> set[str]:
        return {mt for mt, s in self._strategies.items() if s.is_enabled()}

    def is_enabled(self, machine_type: str) -> bool:
        s = self.get(machine_type)
        return s.is_enabled() if s else False

    def workers_per_endpoint(self, machine_type: str) -> int:
        s = self.get(machine_type)
        return s.workers_per_endpoint if s else 1

    def min_frames_per_instance(self, machine_type: str) -> int:
        s = self.get(machine_type)
        return s.min_frames_per_instance if s else 2
