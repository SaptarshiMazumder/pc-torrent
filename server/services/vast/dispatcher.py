"""
VastDispatcher — offer search + instance creation service.

Handles the dispatch flow: find cheapest offer across GPU types, rent it,
persist the instance ID. Also exposes cancel.
"""

from __future__ import annotations

import logging

from infrastructure.db import execute
from services.vast.client import VastApiClient
from services.vast.config import VastConfig
from services.vast.machine_registrar import MachineRegistrar

log = logging.getLogger(__name__)


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
    ) -> int:
        """Find a cheap offer and rent it. Returns the Vast.ai instance ID.

        Tries the GPU type assigned to machine_id first. If no offers are found
        under the price cap, falls back through every other configured GPU type
        in order until one succeeds.
        """
        primary_gpu = self._registrar.gpu_name_for_machine(machine_id)
        all_gpu_names = [ep.gpu_name for ep in self._cfg.endpoints]

        gpu_order = [primary_gpu] + [g for g in all_gpu_names if g != primary_gpu]

        last_err: Exception | None = None
        for gpu_name in gpu_order:
            offers = self._client.search_offers(gpu_name)
            if not offers:
                log.info(
                    f"Vast.ai: no offers for '{gpu_name}' "
                    f"under ${self._cfg.max_price_per_gpu}/hr — trying next GPU type"
                )
                continue

            for offer in offers:
                offer_id = offer.get("id")
                dph = offer.get("dph_total", "?")
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
                    if gpu_name != primary_gpu:
                        log.info(
                            f"Job {job_id}: rented fallback GPU '{gpu_name}' "
                            f"(primary '{primary_gpu}' had no offers)"
                        )
                    log.info(
                        f"Dispatched job {job_id} -> Vast.ai offer {offer_id} "
                        f"({gpu_name}, ${dph}/hr) -> instance {instance_id}"
                    )
                    return instance_id
                except Exception as e:
                    log.warning(f"Failed to rent Vast.ai offer {offer_id} ({gpu_name}): {e}")
                    last_err = e

        raise RuntimeError(
            f"No Vast.ai offers available across all {len(gpu_order)} GPU types "
            f"under ${self._cfg.max_price_per_gpu}/hr. Last error: {last_err}"
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
        """Dispatch to Vast.ai and persist the instance ID as the provider job ID."""
        instance_id = self.dispatch(
            job_id=job_id,
            blend_url=blend_url,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            render_overrides_b64=render_overrides_b64,
            machine_id=machine_id,
        )
        execute("UPDATE jobs SET runpod_job_id = %s WHERE id = %s", (str(instance_id), job_id))
        return str(instance_id)

    def cancel(self, provider_job_id: str) -> None:
        """Destroy the Vast.ai instance (terminates the job)."""
        try:
            instance_id = int(provider_job_id)
            self._client.destroy_instance(instance_id)
        except (ValueError, TypeError) as e:
            log.warning(f"Invalid Vast.ai instance ID '{provider_job_id}': {e}")
