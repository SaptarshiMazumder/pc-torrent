"""Strategy registry — maps machine_type to its ProvisionStrategy."""

from __future__ import annotations

from scheduling.strategies.base import ProvisionStrategy
from scheduling.strategies.community_strategy import CommunityStrategy
from scheduling.strategies.modal_strategy import ModalStrategy
from scheduling.strategies.runpod_strategy import RunPodStrategy
from scheduling.strategies.vast_strategy import VastStrategy

_STRATEGIES: dict[str, ProvisionStrategy] = {}
_FALLBACK = CommunityStrategy()


def _register_defaults() -> None:
    for strategy in (RunPodStrategy(), ModalStrategy(), VastStrategy(), CommunityStrategy()):
        _STRATEGIES[strategy.machine_type] = strategy


def get_strategy(machine_type: str) -> ProvisionStrategy:
    """Return the strategy for *machine_type*, falling back to community."""
    if not _STRATEGIES:
        _register_defaults()
    return _STRATEGIES.get(machine_type, _FALLBACK)


def all_strategies() -> list[ProvisionStrategy]:
    if not _STRATEGIES:
        _register_defaults()
    return list(_STRATEGIES.values())
