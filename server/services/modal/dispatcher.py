"""
ModalDispatcher — dispatch + save + cancel service.

Resolves the machine_id to a GPU type, delegates the HTTP call to the
client, and persists the provider job ID in the database.
"""

from __future__ import annotations

import logging

from services.modal.client import ModalApiClient
from services.modal.config import ModalConfig
from services.modal.job_state import save_function_call_id
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
        save_function_call_id(job_id, modal_job_id)
        return modal_job_id

    def cancel(self, provider_job_id: str) -> None:
        """Cancel a live Modal function call when we have a real call id."""
        provider_job_id = (provider_job_id or "").strip()
        if not provider_job_id:
            log.warning("Modal cancel requested with empty provider job id")
            return
        if provider_job_id.startswith("modal-"):
            log.warning(
                f"Modal cancel requested for synthetic id {provider_job_id}; "
                "redeploy modal_worker/app.py so dispatch returns a real function_call_id"
            )
            return

        try:
            import modal

            modal.FunctionCall.from_id(provider_job_id).cancel(terminate_containers=True)
            log.info(f"Cancelled Modal function call {provider_job_id}")
        except Exception as exc:
            log.warning(f"Failed to cancel Modal function call {provider_job_id}: {exc}")
