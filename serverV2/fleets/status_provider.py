"""Fleet status providers — thin read-only wrappers over InstanceRegistry.

Each implements IInstanceStatusProvider.
"""

from __future__ import annotations

from serverV2.core.models import InstanceSnapshot
from serverV2.fleets.instance_registry import InstanceRegistry


class ModalStatusProvider:

    def __init__(self, registry: InstanceRegistry) -> None:
        self._registry = registry

    @property
    def fleet_type(self) -> str:
        return "modal_serverless"

    def get_all(self) -> list[InstanceSnapshot]:
        return self._registry.get_all()

    def get_by_job(self, job_id: str) -> InstanceSnapshot | None:
        return self._registry.get_by_job(job_id)


class VastStatusProvider:

    def __init__(self, registry: InstanceRegistry) -> None:
        self._registry = registry

    @property
    def fleet_type(self) -> str:
        return "vast_serverless"

    def get_all(self) -> list[InstanceSnapshot]:
        return self._registry.get_all()

    def get_by_job(self, job_id: str) -> InstanceSnapshot | None:
        return self._registry.get_by_job(job_id)
