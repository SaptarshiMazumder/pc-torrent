"""
ModalDispatcher — dispatch + save + cancel service.

Resolves the machine_id to a GPU type, delegates the HTTP call to the
client, and persists the provider job ID in the database.
"""

from __future__ import annotations

import logging

from infrastructure.db import execute
from services.modal.client import ModalApiClient
from services.modal.config import ModalConfig
from services.modal.machine_registrar import MachineRegistrar

log = logging.getLogger(__name__)


class ModalDispatcher:
    def __init__(
        self,
        config: ModalConfig,
        client: ModalApiClient,
        registrar: MachineRegistrar,
    ) -> None:
        self._cfg = config
        self._client = client
        self._registrar = registrar

    def dispatch(
        self,
        job_id: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_b64: str,
        machine_id: str = "",
    ) -> str:
        """POST a job to the correct Modal web endpoint. Returns a job identifier."""
        if not self._cfg.is_enabled():
            raise RuntimeError("Modal provisioning is disabled or not configured")
        gpu_type = self._registrar.gpu_type_for_machine(machine_id)
        return self._client.dispatch(
            gpu_type=gpu_type,
            job_id=job_id,
            blend_url=blend_url,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            render_overrides_b64=render_overrides_b64,
        )

    def dispatch_and_save(
        self,
        job_id: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_b64: str,
        machine_id: str,
    ) -> str:
        """Dispatch to Modal and persist the job ID on the job row."""
        modal_job_id = self.dispatch(
            job_id=job_id,
            blend_url=blend_url,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            render_overrides_b64=render_overrides_b64,
            machine_id=machine_id,
        )
        execute("UPDATE jobs SET runpod_job_id = %s WHERE id = %s", (modal_job_id, job_id))
        return modal_job_id

    def cancel(self, provider_job_id: str) -> None:
        """No-op: Modal cancellation is handled via DB status change."""
        log.info(f"Modal cancel requested for {provider_job_id} (handled via DB status)")
