"""FleetRegistry — maps fleet -> IFleetStrategy.

Strategies are registered at boot time.  Lookup is O(1).  After Phase 1
of the allocator redesign, the registry key is the strategy's ``fleet``
property (was ``machine_type``).  The string values are unchanged
("community", "modal_serverless", "vast_serverless") — only the field
name on the strategy moved.
"""

from __future__ import annotations

import logging

from serverV2.core.interfaces import IFleetStrategy

log = logging.getLogger(__name__)


class FleetRegistry:

    def __init__(self) -> None:
        self._strategies: dict[str, IFleetStrategy] = {}

    def register(self, strategy: IFleetStrategy) -> None:
        self._strategies[strategy.fleet] = strategy
        log.info("Registered fleet strategy: %s", strategy.fleet)

    def get(self, fleet: str) -> IFleetStrategy | None:
        return self._strategies.get(fleet)

    def get_or_raise(self, fleet: str) -> IFleetStrategy:
        strategy = self.get(fleet)
        if strategy is None:
            raise KeyError(f"No fleet strategy for fleet={fleet!r}")
        return strategy

    def all_strategies(self) -> list[IFleetStrategy]:
        return list(self._strategies.values())

    def enabled_fleets(self) -> set[str]:
        return {f for f, s in self._strategies.items() if s.is_enabled()}

    def is_enabled(self, fleet: str) -> bool:
        s = self.get(fleet)
        return s.is_enabled() if s else False

    def min_frames_per_instance(self, fleet: str) -> int:
        s = self.get(fleet)
        return s.min_frames_per_instance if s else 2
