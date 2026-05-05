"""ModalFleetStrategy — composes ModalClient + ModalMonitorManager + JobRepository.

Implements IFleetStrategy via composition.  After Phase 1 of the
allocator redesign, the strategy reads ``task.gpu_type`` directly from
the planned task — no machine_id → gpu_type lookup needed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from serverV2.config import ModalConfig
from serverV2.core.models import CreateJobParams, DispatchContext, DispatchResult, PlannedTask
from serverV2.fleets.modal.client import ModalClient
from serverV2.fleets.modal.modal_active_jobs_hooks import ModalActiveJobsHooks
from serverV2.fleets.modal.monitor import ModalMonitorManager
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)

_FLEET = "modal_serverless"


class ModalFleetStrategy:

    def __init__(
        self,
        config: ModalConfig,
        client: ModalClient,
        callback_handler: ModalMonitorManager,
        job_repo: JobRepository,
        on_failure: Callable[[str, str], None],
        active_jobs_hooks: ModalActiveJobsHooks,
    ) -> None:
        self._cfg = config
        self._client = client
        self._callback = callback_handler
        self._job_repo = job_repo
        self._on_failure = on_failure
        self._active_jobs_hooks = active_jobs_hooks

    @property
    def fleet(self) -> str:
        return _FLEET

    @property
    def min_frames_per_instance(self) -> int:
        return 5

    def is_enabled(self) -> bool:
        return self._cfg.is_enabled()

    def dispatch(self, task: PlannedTask, context: DispatchContext, job_id: str) -> DispatchResult:
        if not task.gpu_type:
            raise RuntimeError(
                f"Modal dispatch requires task.gpu_type to be set; got None for job {job_id}"
            )
        gpu_type = task.gpu_type

        # Snapshot price at dispatch time for telemetry — looked up from
        # config so the row is immune to later config edits.
        price_at_dispatch = next(
            (ep.price_per_hour for ep in self._cfg.endpoints if ep.gpu_type == gpu_type),
            None,
        )

        self._job_repo.create(CreateJobParams(
            job_id=job_id,
            fleet=_FLEET,
            machine_id=None,
            gpu_type=gpu_type,
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
            price_per_hour_at_dispatch=price_at_dispatch,
            estimated_seconds=task.estimated_seconds,
            estimated_cost_usd=task.estimated_cost_usd,
            estimated_seconds_per_frame=task.estimated_seconds_per_frame,
            estimated_startup_seconds=task.estimated_startup_seconds,
        ))
        # Mirror the new active job into the Redis live-count index used
        # by ModalAvailabilityBuilder for cap enforcement.  Cleanup on
        # any terminal transition is handled by the lifecycle handlers
        # firing ``on_terminal``; SREM is idempotent so the dispatch-
        # failure path (which routes through handle_chunk_failed) does
        # the right thing without needing explicit undo here.
        self._active_jobs_hooks.on_dispatch(job_id=job_id, gpu_type=gpu_type)

        try:
            provider_job_id = self._client.dispatch_job(
                job_id=job_id,
                gpu_type=gpu_type,
                blend_url=context.blend_url,
                frame_start=task.frame_start,
                frame_end=task.frame_end,
                frame_step=task.frame_step,
                render_overrides_json=context.render_overrides_json,
            )
            self._job_repo.save_provider_job_id(
                job_id, provider_job_id=provider_job_id, column="modal_function_call_id",
            )
        except Exception as exc:
            log.error("Modal dispatch failed for %s: %s", job_id, exc)
            error = f"Dispatch failed: {exc}"
            self._on_failure(job_id, error)
            return DispatchResult(job_id=job_id, machine_id="", status="failed", error=error)

        self._callback.start_monitoring(
            job_id=job_id,
            provider_job_id=provider_job_id,
            machine_id="",
            blend_url=context.blend_url,
            render_overrides_json=context.render_overrides_json,
            group_id=context.group_id,
            estimated_startup_sec=task.estimated_startup_seconds,
        )

        return DispatchResult(
            job_id=job_id,
            machine_id="",
            status="pending",
            provider_job_id=provider_job_id,
        )

    def cancel(self, provider_job_id: str) -> None:
        self._client.cancel_job(provider_job_id)

    def stop_monitoring(self, job_id: str) -> None:
        self._callback.stop_monitoring(job_id)

    def provider_job_id_from_job(self, job: dict[str, Any]) -> str | None:
        return (job.get("modal_function_call_id") or "").strip() or None
