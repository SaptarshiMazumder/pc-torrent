"""ModalFleetStrategy — composes ModalClient + ModalCallbackHandler + JobRepository.

Implements IFleetStrategy via composition.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from serverV2.config import ModalConfig
from serverV2.core.models import CreateJobParams, DispatchContext, DispatchResult, PlannedTask
from serverV2.fleets.modal.client import ModalClient
from serverV2.fleets.modal.callback import ModalCallbackHandler
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)


class ModalFleetStrategy:

    def __init__(
        self,
        config: ModalConfig,
        client: ModalClient,
        callback_handler: ModalCallbackHandler,
        job_repo: JobRepository,
        gpu_type_lookup: Callable[[str], str],
        on_failure: Callable[[str, str], None] | None = None,
    ) -> None:
        self._cfg = config
        self._client = client
        self._callback = callback_handler
        self._job_repo = job_repo
        self._gpu_type_lookup = gpu_type_lookup
        self._on_failure = on_failure

    @property
    def machine_type(self) -> str:
        return "modal_serverless"

    @property
    def workers_per_endpoint(self) -> int:
        return self._cfg.workers_per_endpoint

    @property
    def min_frames_per_instance(self) -> int:
        return 5

    def is_enabled(self) -> bool:
        return self._cfg.is_enabled()

    def dispatch(self, task: PlannedTask, context: DispatchContext, job_id: str) -> DispatchResult:
        self._job_repo.create(CreateJobParams(
            job_id=job_id,
            machine_id=task.machine_id,
            group_id=context.group_id,
            input_filename=context.input_filename,
            total_frames=task.total_frames,
            frame_start=task.frame_start,
            frame_end=task.frame_end,
            frame_step=task.frame_step,
            render_overrides_json=context.render_overrides_b64,
            max_retries=context.max_retries,
            priority=context.priority,
            chunk_index=task.chunk_index,
            attempt=task.attempt,
        ))

        try:
            gpu_type = self._gpu_type_lookup(task.machine_id)
            provider_job_id = self._client.dispatch_job(
                job_id=job_id,
                gpu_type=gpu_type,
                blend_url=context.blend_url,
                frame_start=task.frame_start,
                frame_end=task.frame_end,
                frame_step=task.frame_step,
                render_overrides_b64=context.render_overrides_b64,
            )
            self._job_repo.save_provider_job_id(
                job_id, provider_job_id=provider_job_id, column="modal_function_call_id",
            )
        except Exception as exc:
            log.error("Modal dispatch failed for %s: %s", job_id, exc)
            error = f"Dispatch failed: {exc}"
            if self._on_failure:
                self._on_failure(job_id, error)
            else:
                self._job_repo.mark_failed(job_id, error)
            return DispatchResult(job_id=job_id, machine_id=task.machine_id, status="failed", error=error)

        self._callback.start_monitoring(
            job_id=job_id,
            provider_job_id=provider_job_id,
            machine_id=task.machine_id,
            blend_url=context.blend_url,
            render_overrides_b64=context.render_overrides_b64,
            group_id=context.group_id,
        )

        return DispatchResult(
            job_id=job_id,
            machine_id=task.machine_id,
            status="pending",
            provider_job_id=provider_job_id,
        )

    def cancel(self, provider_job_id: str, machine_id: str) -> None:
        self._client.cancel_job(provider_job_id)

    def stop_monitoring(self, job_id: str) -> None:
        self._callback.stop_monitoring(job_id)

    def provider_job_id_from_job(self, job: dict[str, Any]) -> str | None:
        return (job.get("modal_function_call_id") or "").strip() or None
