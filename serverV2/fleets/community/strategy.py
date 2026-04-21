"""Community (physical desktop) fleet strategy.

Desktop agents poll the server for pending jobs, so dispatch is a no-op.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import DispatchContext, DispatchResult, PlannedTask


class CommunityStrategy:

    @property
    def machine_type(self) -> str:
        return "windows"

    @property
    def workers_per_endpoint(self) -> int:
        return 1

    @property
    def min_frames_per_instance(self) -> int:
        return 2

    def is_enabled(self) -> bool:
        return True

    def dispatch(self, task: PlannedTask, context: DispatchContext, job_id: str) -> DispatchResult:
        return DispatchResult(
            job_id=job_id,
            machine_id=task.machine_id,
            status="pending",
        )

    def cancel(self, provider_job_id: str, machine_id: str) -> None:
        pass

    def stop_monitoring(self, job_id: str) -> None:
        pass

    def provider_job_id_from_job(self, job: dict[str, Any]) -> str | None:
        return None
