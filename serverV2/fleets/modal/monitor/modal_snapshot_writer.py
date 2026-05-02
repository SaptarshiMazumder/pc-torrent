"""ModalSnapshotWriter — writes per-job InstanceSnapshot into the shared
InstanceRegistry for the debug/UI read path.  Pure mapping, no decisions.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import InstanceSnapshot
from serverV2.fleets.instance_registry import InstanceRegistry


class ModalSnapshotWriter:

    FLEET_TYPE = "modal_serverless"

    def __init__(self, job_id: str, registry: InstanceRegistry | None) -> None:
        self._job_id = job_id
        self._registry = registry

    def write(self, job: dict[str, Any], elapsed: float) -> None:
        if not self._registry:
            return
        self._registry.update(self._job_id, InstanceSnapshot(
            job_id=self._job_id,
            fleet_type=self.FLEET_TYPE,
            provider_status=str(job.get("status") or "pending"),
            gpu_label=str(job.get("gpu_model") or "Modal GPU"),
            rendered_frames=job.get("rendered_frames") or 0,
            total_frames=job.get("total_frames") or 0,
            elapsed_sec=round(elapsed),
            error=job.get("error"),
        ))

    def remove(self) -> None:
        if self._registry:
            self._registry.remove(self._job_id)
