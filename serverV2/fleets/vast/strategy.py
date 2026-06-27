"""VastFleetStrategy — composes VastClient + JobRepository.

Implements IFleetStrategy via composition, not inheritance.  Reads
``task.gpu_type`` directly (the GPU name string Vast expects) — no
machine_id → gpu_name lookup needed.

Monitor lifecycle is owned by the singleton ``VastFleetMonitor``, not
by this strategy.  ``dispatch`` writes the row + provider job id and
returns; the singleton's next 30s sweep observes the row and starts
running its decision blocks.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from serverV2.allocation.allowed_stall_times_resolver import AllowedStallTimesResolver
from serverV2.config.vast.providers.vast_runtime_config_provider import (
    VastRuntimeConfigProvider,
)
from serverV2.core.models import CreateJobParams, DispatchContext, DispatchResult, PlannedTask
from serverV2.fleets.vast.client import VastClient
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)

_FLEET = "vast_serverless"


class VastFleetStrategy:

    def __init__(
        self,
        config_provider: VastRuntimeConfigProvider,
        client: VastClient,
        job_repo: JobRepository,
        on_failure: Callable[[str, str], None],
        allowed_stall_times_resolver: AllowedStallTimesResolver,
    ) -> None:
        self._config_provider = config_provider
        self._client = client
        self._job_repo = job_repo
        self._on_failure = on_failure
        self._stall_resolver = allowed_stall_times_resolver

    @property
    def fleet(self) -> str:
        return _FLEET

    @property
    def min_frames_per_instance(self) -> int:
        return 10

    def is_enabled(self) -> bool:
        return self._config_provider.get().is_enabled()

    def dispatch(self, task: PlannedTask, context: DispatchContext, job_id: str) -> DispatchResult:
        if not task.gpu_type:
            raise RuntimeError(
                f"Vast dispatch requires task.gpu_type to be set; got None for job {job_id}"
            )
        if task.offer_id is None:
            raise RuntimeError(
                f"Vast dispatch requires task.offer_id to be set; got None for job {job_id}. "
                f"Per-offer ranking is the source of truth — the planner must pick a specific offer."
            )
        gpu_name = task.gpu_type

        # Real per-offer price at dispatch time, captured by the planner
        # from the live Vast bundle.  Stamped onto the row for telemetry.
        price_at_dispatch = task.price_per_hour or None

        allowed_stall_times = self._stall_resolver.resolve(
            fleet=_FLEET,
            group_id=context.group_id,
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
            allowed_stall_times=allowed_stall_times,
        ))

        try:
            image = self._config_provider.get().image_for_engine(context.engine)
            log.info(
                "[ALLOC] dispatching vast offer=%s gpu=%s cuda=%s os=%s price=$%.3f/hr job=%s",
                task.offer_id, gpu_name, task.cuda_version, task.host_os,
                task.price_per_hour, job_id,
            )
            instance_id = self._client.dispatch_job(
                job_id=job_id,
                blend_url=context.blend_url,
                frame_start=task.frame_start,
                frame_end=task.frame_end,
                frame_step=task.frame_step,
                render_overrides_json=context.render_overrides_json,
                offer_id=task.offer_id,
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

        # No per-job monitor thread spawn -- the singleton VastFleetMonitor
        # picks up this row on its next 30s sweep.
        return DispatchResult(
            job_id=job_id,
            machine_id="",
            status="pending",
            provider_job_id=provider_job_id,
        )

    def cancel(self, provider_job_id: str) -> None:
        self._client.cancel_job(provider_job_id)

    def stop_monitoring(self, job_id: str) -> None:
        # No-op: the singleton VastFleetMonitor drops per-job state on
        # the next tick when it sees the row has gone terminal.
        return

    def provider_job_id_from_job(self, job: dict[str, Any]) -> str | None:
        value = (job.get("vast_job_id") or "").strip()
        return value or None
