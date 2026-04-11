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
        """Cancel a live Modal function call and terminate its container.

        Retries up to _CANCEL_RETRIES times with backoff so transient API
        failures don't leave containers running.
        """
        import time

        _CANCEL_RETRIES = 3
        _CANCEL_BACKOFF_SEC = 3.0

        provider_job_id = (provider_job_id or "").strip()
        if not provider_job_id:
            log.warning("Modal cancel requested with empty provider job id")
            return
        if provider_job_id.startswith("modal-"):
            log.error(
                "MODAL CONTAINER LEAK — cancel requested for synthetic id %s. "
                "The container cannot be terminated because no real function_call_id "
                "was captured at dispatch time. Redeploy modal_worker/app.py so the "
                "endpoint returns a function_call_id in the response body.",
                provider_job_id,
            )
            return

        import modal

        last_exc: Exception | None = None
        for attempt in range(1, _CANCEL_RETRIES + 1):
            try:
                modal.FunctionCall.from_id(provider_job_id).cancel(terminate_containers=True)
                log.info(
                    "Cancelled Modal function call %s (terminate_containers=True, attempt %d)",
                    provider_job_id,
                    attempt,
                )
                return
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "Modal cancel attempt %d/%d failed for %s: %s",
                    attempt,
                    _CANCEL_RETRIES,
                    provider_job_id,
                    exc,
                )
                if attempt < _CANCEL_RETRIES:
                    time.sleep(_CANCEL_BACKOFF_SEC)

        log.error(
            "MODAL CONTAINER LEAK — failed to cancel %s after %d attempts. "
            "Container may still be running and consuming GPU. Last error: %s",
            provider_job_id,
            _CANCEL_RETRIES,
            last_exc,
        )
