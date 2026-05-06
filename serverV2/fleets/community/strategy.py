"""Community (physical desktop) fleet strategy.

Desktop agents poll ``GET /jobs/next-for-machine/{id}`` for pending rows
keyed to their ``machine_id``.  Dispatch's only job here is to **insert
that row** — there's no provider API to call (the agent on the user's PC
is the provider).  Mirrors how ``VastFleetStrategy`` and
``ModalFleetStrategy`` insert before doing their provider calls.

The previous v2 implementation was a no-op return that skipped the
INSERT entirely, which is why community jobs disappeared on dispatch.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import (
    CreateJobParams,
    DispatchContext,
    DispatchResult,
    PlannedTask,
)
from serverV2.repositories.job_repository import JobRepository
from serverV2.services.machines.machine_repository import MachineRepository


_FLEET = "community"


class CommunityStrategy:

    def __init__(
        self,
        *,
        job_repo: JobRepository,
        machine_repo: MachineRepository,
    ) -> None:
        self._job_repo = job_repo
        self._machine_repo = machine_repo

    @property
    def fleet(self) -> str:
        return _FLEET

    @property
    def min_frames_per_instance(self) -> int:
        return 2

    def is_enabled(self) -> bool:
        return True

    def dispatch(self, task: PlannedTask, context: DispatchContext, job_id: str) -> DispatchResult:
        if not task.machine_id:
            raise RuntimeError(
                f"Community dispatch requires task.machine_id; got None for job {job_id}"
            )
        self._job_repo.create(CreateJobParams(
            job_id=job_id,
            fleet=_FLEET,
            machine_id=task.machine_id,
            gpu_type=None,                     # community machines aren't gpu_type-keyed
            group_id=context.group_id,
            input_filename=context.input_filename,
            total_frames=task.total_frames,
            frame_start=task.frame_start,
            frame_end=task.frame_end,
            frame_step=task.frame_step,
            render_overrides_json=context.render_overrides_json,
            max_retries=context.max_retries,
            priority=context.priority,
            chunk_index=task.chunk_index,
            attempt=task.attempt,
            price_per_hour_at_dispatch=None,   # telemetry skipped for community in v1
            estimated_seconds=task.estimated_seconds,
            estimated_cost_usd=task.estimated_cost_usd,
            estimated_seconds_per_frame=task.estimated_seconds_per_frame,
            estimated_startup_seconds=task.estimated_startup_seconds,
        ))
        # Flip the machine to 'processing' on dispatch (not on agent claim).
        # Closes the dispatch -> claim race window: subsequent allocator
        # ticks read 'machines:status' from Redis and exclude this machine
        # immediately, instead of seeing it as 'available' for the
        # 5-15s gap until the agent's next poll.
        self._machine_repo.update_status(task.machine_id, "processing")
        return DispatchResult(
            job_id=job_id,
            machine_id=task.machine_id,
            status="pending",
        )

    def cancel(self, provider_job_id: str) -> None:
        # No provider API to call — there's no Vast-style "destroy" here.
        # Lifecycle.cancel_render has already marked the job's DB row
        # ``cancelled`` before this method is called.  The community
        # agent polls ``GET /jobs/{id}/cancel-status`` every 30s during
        # render and kills its Docker container locally on detected
        # cancel — see agent/agent.py start_cancel_check_loop.
        pass

    def stop_monitoring(self, job_id: str) -> None:
        # CommunityMonitor is a single scanning daemon (not per-job
        # threads like Vast/Modal), so there's nothing to stop here.
        pass

    def provider_job_id_from_job(self, job: dict[str, Any]) -> str | None:
        return None
