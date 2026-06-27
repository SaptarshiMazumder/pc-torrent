"""ModalAvailabilityBuilder — Modal-fleet availability.

Returns the set of Modal endpoints that have headroom under both the
per-GPU cap (``ModalConfig.per_gpu_max_parallel``) and the fleet-wide
cap (``ModalConfig.max_parallel``).

Counts come from the Redis-backed ``ModalActiveJobsTracker`` first;
when Redis is unreachable, falls back to
``JobRepository.count_active_by_fleet_and_gpu_type``.  The two sources
are consistent because every Modal dispatch fires the tracker's
``add`` alongside the PG ``create``, and every Modal terminal transition
fires the tracker's ``remove`` alongside the PG status update.

No HTTP -- Modal has no availability endpoint per
``serverV2/umls/plans/fleet_capacity_preflight.md``.  The "stay under
your account's effective cap, trust the retry pipeline + queue
timeout for the rare overage" story is encoded as the two configured
caps.
"""

from __future__ import annotations

from serverV2.config.modal.providers.modal_runtime_config_provider import (
    ModalRuntimeConfigProvider,
)
from serverV2.core.models import FleetCapability
from serverV2.fleets.modal.modal_active_jobs_tracker import (
    ModalActiveJobsTracker,
)
from serverV2.repositories.job_repository import JobRepository


_FLEET = "modal_serverless"


class ModalAvailabilityBuilder:

    def __init__(
        self,
        *,
        config_provider: ModalRuntimeConfigProvider,
        job_repo: JobRepository,
        tracker: ModalActiveJobsTracker,
    ) -> None:
        self._config_provider = config_provider
        self._jobs = job_repo
        self._tracker = tracker

    def build(self) -> tuple[FleetCapability, ...]:
        cfg = self._config_provider.get()
        gpu_types = [ep.gpu_type for ep in cfg.endpoints]

        # Redis-first.  Falls back to PG when Redis is unreachable;
        # when Redis is up, this is one MGET-style round-trip per gpu.
        counts = self._tracker.count_for_gpu_types(gpu_types)
        if counts is None:
            pg_counts = self._jobs.count_active_by_fleet_and_gpu_type()
            counts = {
                gt: pg_counts.get((_FLEET, gt), 0) for gt in gpu_types
            }

        total = sum(counts.values())
        if total >= cfg.max_parallel:
            return ()

        available: list[FleetCapability] = []
        for ep in cfg.endpoints:
            if counts.get(ep.gpu_type, 0) >= cfg.per_gpu_max_parallel:
                continue
            available.append(FleetCapability(
                fleet=_FLEET,
                gpu_type=ep.gpu_type,
                label=ep.label,
                vram_gb=ep.vram_gb,
                cpu_cores=ep.cpu_cores,
                ram_gb=ep.ram_gb,
                render_speed=ep.render_speed,
                fleet_max_parallel=cfg.max_parallel,
                price_per_hour=ep.price_per_hour,
                available_seconds=cfg.availability_sec,
            ))
        return tuple(available)
