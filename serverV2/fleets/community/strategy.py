"""Community (physical desktop) fleet strategy.

Desktop agents poll the server for pending jobs, so dispatch is a no-op
on this side — the job row already exists (created at upload time) and
the agent picks it up by ``machine_id``.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import DispatchContext, DispatchResult, PlannedTask


_FLEET = "community"


class CommunityStrategy:

    @property
    def fleet(self) -> str:
        return _FLEET

    @property
    def min_frames_per_instance(self) -> int:
        return 2

    def is_enabled(self) -> bool:
        return True

    def dispatch(self, task: PlannedTask, context: DispatchContext, job_id: str) -> DispatchResult:
        return DispatchResult(
            job_id=job_id,
            machine_id=task.machine_id or "",
            status="pending",
        )

    def cancel(self, provider_job_id: str) -> None:
        pass

    def stop_monitoring(self, job_id: str) -> None:
        pass

    def provider_job_id_from_job(self, job: dict[str, Any]) -> str | None:
        return None
