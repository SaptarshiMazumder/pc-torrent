"""VastAvailabilityBuilder — Vast-fleet availability, per-offer.

Returns one ``FleetCapability`` per live rentable offer.  Each offer is
ranked on its own merits — its actual ``dph_total``, driver-reported
CUDA version, and OS — rather than being aggregated to one entry per
gpu_type.

Fleet-cap check is one DB query (``count_active_by_fleet``).  Per-GPU
availability is N independent ``GET /bundles/`` calls -- one per
configured ``VastEndpoint`` -- fanned out via a thread pool so latency
stays bounded by the slowest individual call rather than scaling
linearly with endpoint count.

Per-call exceptions are absorbed by ``_safe_search`` and treated as
"this gpu_type isn't available right now."  A transient Vast hiccup on
one GPU type doesn't poison the whole probe.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from serverV2.config import VastEndpoint
from serverV2.config.vast.providers.vast_runtime_config_provider import (
    VastRuntimeConfigProvider,
)
from serverV2.core.models import FleetCapability
from serverV2.fleets.vast.client import VastClient
from serverV2.fleets.vast.vast_offer import VastOffer
from serverV2.repositories.job_repository import JobRepository


_FLEET = "vast_serverless"
_MAX_WORKERS = 8

log = logging.getLogger(__name__)


class VastAvailabilityBuilder:

    def __init__(
        self,
        *,
        client: VastClient,
        config_provider: VastRuntimeConfigProvider,
        job_repo: JobRepository,
    ) -> None:
        self._client = client
        self._config_provider = config_provider
        self._jobs = job_repo

    def build(self) -> tuple[FleetCapability, ...]:
        cfg = self._config_provider.get()
        # Fleet-cap short-circuit -- skip the marketplace probe if we're
        # already at our self-imposed concurrency ceiling.
        in_flight = self._jobs.count_active_by_fleet()
        if in_flight.get(_FLEET, 0) >= cfg.max_parallel:
            return ()

        endpoints = list(cfg.endpoints)
        if not endpoints:
            return ()

        # Parallel /bundles/ probe -- one fan-out per gpu_type endpoint.
        workers = min(len(endpoints), _MAX_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            offer_lists = list(pool.map(self._safe_search, endpoints))

        available: list[FleetCapability] = []
        for ep, offers in zip(endpoints, offer_lists):
            for offer in offers:
                available.append(FleetCapability(
                    fleet=_FLEET,
                    gpu_type=ep.gpu_name,
                    label=ep.label,
                    vram_gb=ep.vram_gb,
                    cpu_cores=ep.cpu_cores,
                    ram_gb=ep.ram_gb,
                    render_speed=ep.render_speed,
                    fleet_max_parallel=cfg.max_parallel,
                    # Per-offer fields -- the marketplace truth, not config
                    price_per_hour=offer.dph_total,
                    offer_id=offer.offer_id,
                    cuda_version=offer.cuda_version,
                    host_os=offer.host_os,
                    available_seconds=offer.duration_sec,
                ))
        return tuple(available)

    def _safe_search(self, ep: VastEndpoint) -> list[VastOffer]:
        """Per-thread Vast offer search.  Treats any HTTP/network error
        as 'this gpu_type isn't available right now' and returns ``[]``.
        """
        try:
            return self._client.offers.search(ep.gpu_name)
        except Exception as exc:
            log.warning(
                "VastAvailabilityBuilder: offers.search(%s) failed: %s",
                ep.gpu_name, exc,
            )
            return []
