"""VastFleetStrategy — composes VastClient + VastMonitorManager + JobRepository.

Implements IFleetStrategy via composition, not inheritance.  After Phase
1 of the allocator redesign, the strategy reads ``task.gpu_type``
directly (the GPU name string Vast expects) — no machine_id → gpu_name
lookup needed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from serverV2.config import VastConfig
from serverV2.core.models import CreateJobParams, DispatchContext, DispatchResult, PlannedTask
from serverV2.fleets.vast.client import VastClient
from serverV2.fleets.vast.monitor import VastMonitorManager
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)

_FLEET = "vast_serverless"


class VastFleetStrategy:

    def __init__(
        self,
        config: VastConfig,
        client: VastClient,
        callback_handler: VastMonitorManager,
        job_repo: JobRepository,
        on_failure: Callable[[str, str], None],
    ) -> None:
        self._cfg = config
        self._client = client
        self._callback = callback_handler
        self._job_repo = job_repo
        self._on_failure = on_failure

    @property
    def fleet(self) -> str:
        return _FLEET

    @property
    def min_frames_per_instance(self) -> int:
        return 10

    def is_enabled(self) -> bool:
        return self._cfg.is_enabled()

    def dispatch(self, task: PlannedTask, context: DispatchContext, job_id: str) -> DispatchResult:
        if not task.gpu_type:
            raise RuntimeError(
                f"Vast dispatch requires task.gpu_type to be set; got None for job {job_id}"
            )
        gpu_name = task.gpu_type

        # Snapshot price at dispatch time for telemetry — looked up from
        # config so the row is immune to later config edits.
        price_at_dispatch = next(
            (ep.price_per_hour for ep in self._cfg.endpoints if ep.gpu_name == gpu_name),
            None,
        )

        self._job_repo.create(CreateJobParams(
            job_id=job_id,
            fleet=_FLEET,
            machine_id=None,
            gpu_type=gpu_name,
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

        try:
            image = self._cfg.image_for_engine(context.engine)
            instance_id = self._client.dispatch_job(
                job_id=job_id,
                blend_url=context.blend_url,
                frame_start=task.frame_start,
                frame_end=task.frame_end,
                frame_step=task.frame_step,
                render_overrides_json=context.render_overrides_json,
                gpu_name=gpu_name,
                image=image,
            )
            provider_job_id = str(instance_id)
            self._job_repo.save_provider_job_id(
                job_id, provider_job_id=provider_job_id, column="vast_job_id",
            )
        except Exception as exc:
            log.error("Vast dispatch failed for %s: %s", job_id, exc)
            error = f"Dispatch failed: {exc}"
            log.info("[RETRY_DEBUG] vast.strategy.dispatch: about to call on_failure(%s)", job_id)
            try:
                self._on_failure(job_id, error)
                log.info("[RETRY_DEBUG] vast.strategy.dispatch: on_failure(%s) returned cleanly", job_id)
            except Exception as cb_exc:
                log.error("[RETRY_DEBUG] vast.strategy.dispatch: on_failure(%s) RAISED: %r", job_id, cb_exc)
                raise
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
        value = (job.get("vast_job_id") or "").strip()
        return value or None
