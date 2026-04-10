"""
VastDispatcher — offer search + instance creation service.

Handles the dispatch flow: find cheapest offer for the requested GPU type,
rent it, persist the instance ID and actual GPU info. Also exposes cancel.

No fallback across GPU types — if the requested GPU has no offers, the
dispatch fails and the retry/failover mechanism assigns to a different
machine type with its own correct frame budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from infrastructure.db import execute
from services.vast.client import VastApiClient
from services.vast.config import VastConfig
from services.vast.machine_registrar import MachineRegistrar

log = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    instance_id: int
    actual_gpu_name: str
    actual_gpu_vram_gb: float
    dph_total: float


class VastDispatcher:
    def __init__(
        self,
        config: VastConfig,
        client: VastApiClient,
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
    ) -> DispatchResult:
        """Find a cheap offer for the assigned GPU and rent it.

        Only searches for the GPU type mapped to machine_id.  If no offers
        exist, raises RuntimeError so the job can be retried on a different
        machine type (preserving correct frame budgets and labels).
        """
        gpu_name = self._registrar.gpu_name_for_machine(machine_id)

        offers = self._client.search_offers(gpu_name)
        if not offers:
            raise RuntimeError(
                f"No Vast.ai offers for '{gpu_name}' "
                f"under ${self._cfg.max_price_per_gpu}/hr"
            )

        last_err: Exception | None = None
        for offer in offers:
            offer_id = offer.get("id")
            dph = offer.get("dph_total", 0)
            try:
                instance_id = self._client.create_instance(
                    offer_id=offer_id,
                    job_id=job_id,
                    blend_url=blend_url,
                    frame_start=frame_start,
                    frame_end=frame_end,
                    frame_step=frame_step,
                    render_overrides_b64=render_overrides_b64,
                )
                offer_gpu = offer.get("gpu_name") or gpu_name
                offer_vram_mb = offer.get("gpu_ram") or 0
                offer_vram_gb = round(offer_vram_mb / 1024, 1) if offer_vram_mb > 100 else offer_vram_mb

                log.info(
                    f"Dispatched job {job_id} -> Vast.ai offer {offer_id} "
                    f"(requested={gpu_name}, actual={offer_gpu} {offer_vram_gb}GB, "
                    f"${dph}/hr) -> instance {instance_id}"
                )
                return DispatchResult(
                    instance_id=instance_id,
                    actual_gpu_name=offer_gpu,
                    actual_gpu_vram_gb=offer_vram_gb,
                    dph_total=float(dph),
                )
            except Exception as e:
                log.warning(f"Failed to rent Vast.ai offer {offer_id} ({gpu_name}): {e}")
                last_err = e

        raise RuntimeError(
            f"All {len(offers)} Vast.ai offers for '{gpu_name}' failed to rent. "
            f"Last error: {last_err}"
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
        """Dispatch to Vast.ai, persist the instance ID and actual GPU info."""
        result = self.dispatch(
            job_id=job_id,
            blend_url=blend_url,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            render_overrides_b64=render_overrides_b64,
            machine_id=machine_id,
        )
        execute(
            """UPDATE jobs
               SET runpod_job_id = %s,
                   actual_gpu_name = %s,
                   actual_gpu_vram_gb = %s
               WHERE id = %s""",
            (str(result.instance_id), result.actual_gpu_name,
             result.actual_gpu_vram_gb, job_id),
        )
        return str(result.instance_id)

    def cancel(self, provider_job_id: str) -> None:
        """Destroy the Vast.ai instance (terminates the job)."""
        try:
            instance_id = int(provider_job_id)
            self._client.destroy_instance(instance_id)
        except (ValueError, TypeError) as e:
            log.warning(f"Invalid Vast.ai instance ID '{provider_job_id}': {e}")
