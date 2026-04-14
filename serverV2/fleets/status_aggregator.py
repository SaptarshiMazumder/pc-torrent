"""InstanceStatusAggregator — collects all fleet status providers.

Fleet-agnostic query surface for the API layer.
"""

from __future__ import annotations

from serverV2.core.interfaces import IInstanceStatusProvider
from serverV2.core.models import InstanceSnapshot


class InstanceStatusAggregator:

    def __init__(self) -> None:
        self._providers: list[IInstanceStatusProvider] = []

    def register(self, provider: IInstanceStatusProvider) -> None:
        self._providers.append(provider)

    def get_all(self) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        for provider in self._providers:
            result[provider.fleet_type] = [
                snap.to_dict() for snap in provider.get_all()
            ]
        return result

    def get_by_fleet(self, fleet_type: str) -> list[dict]:
        for provider in self._providers:
            if provider.fleet_type == fleet_type:
                return [snap.to_dict() for snap in provider.get_all()]
        return []
