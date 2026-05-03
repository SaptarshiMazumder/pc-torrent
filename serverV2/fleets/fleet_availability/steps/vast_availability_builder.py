"""VastAvailabilityBuilder — Vast-fleet availability.

Returns the set of Vast endpoints that have at least one rentable
offer right now AND keeps total in-flight Vast jobs under
``VastConfig.max_parallel``.

Fleet-cap check is one DB query (``count_active_by_fleet``).  Per-GPU
availability is N independent ``GET /bundles/`` calls -- one per
configured ``VastEndpoint`` -- fanned out via a thread pool so
latency stays bounded by the slowest individual call rather than
scaling linearly with endpoint count.

Per-call exceptions are absorbed by ``_safe_search`` and treated as
"this gpu_type isn't available right now" -- a transient Vast hiccup
on one GPU type doesn't poison the whole probe.  Every endpoint
produces a result before the FleetCapability assembly runs; partial
failures degrade gracefully to "fewer available endpoints," not
"crashed availability check."
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from serverV2.config import VastConfig, VastEndpoint
from serverV2.core.models import FleetCapability
from serverV2.fleets.vast.client import VastClient
from serverV2.repositories.job_repository import JobRepository


_FLEET = "vast_serverless"
_MAX_WORKERS = 8

log = logging.getLogger(__name__)


class VastAvailabilityBuilder:

    def __init__(
        self,
        *,
        client: VastClient,
        config: VastConfig,
        job_repo: JobRepository,
    ) -> None:
        self._client = client
        self._config = config
        self._jobs = job_repo

    def build(self) -> tuple[FleetCapability, ...]:
        # Fleet-cap short-circuit -- skip the marketplace probe if we're
        # already at our self-imposed concurrency ceiling.
        in_flight = self._jobs.count_active_by_fleet()
        if in_flight.get(_FLEET, 0) >= self._config.max_parallel:
            return ()

        endpoints = list(self._config.endpoints)
        if not endpoints:
            return ()

        # Parallel /bundles/ probe.  ``list(pool.map(...))`` blocks until
        # every submitted task has produced a result; the ``with`` block
        # then calls shutdown(wait=True) on exit.  By the time we reach
        # the FleetCapability assembly below, every endpoint has been
        # checked (success or absorbed-failure) -- no partial state.
        workers = min(len(endpoints), _MAX_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            offer_lists = list(pool.map(self._safe_search, endpoints))

        available: list[FleetCapability] = []
        for ep, offers in zip(endpoints, offer_lists):
            if not offers:
                continue
            available.append(FleetCapability(
                fleet=_FLEET,
                gpu_type=ep.gpu_name,
                label=ep.label,
                vram_gb=ep.vram_gb,
                cpu_cores=ep.cpu_cores,
                ram_gb=ep.ram_gb,
                render_speed=ep.render_speed,
                fleet_max_parallel=self._config.max_parallel,
                price_per_hour=ep.price_per_hour,
            ))
        return tuple(available)

    def _safe_search(self, ep: VastEndpoint) -> list[dict[str, Any]]:
        """Per-thread Vast offer search.  Treats any HTTP/network error
        as 'this gpu_type isn't available right now' and returns ``[]``.
        Empty result is a normal Vast signal (drained marketplace) and
        doesn't go through this branch -- offers.search returns ``[]``
        directly on a 200-with-no-results response.
        """
        try:
            return self._client.offers.search(ep.gpu_name)
        except Exception as exc:
            log.warning(
                "VastAvailabilityBuilder: offers.search(%s) failed: %s",
                ep.gpu_name, exc,
            )
            return []
