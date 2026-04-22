"""DefaultFrameAllocator — the current allocation behaviour.

Initial: distribute + expand serverless, honouring fleet enablement.
Retry:   highest ``compute_power_score`` among the available pool.

Swap this implementation to introduce tier / GPU / price rules without
touching the lifecycle or the coordinator.
"""

from __future__ import annotations

from serverV2.allocation.frame_distributor import distribute_frames
from serverV2.allocation.power_scorer import compute_power_score
from serverV2.allocation.serverless_expander import expand_serverless_assignments
from serverV2.core.models import Machine, PlannedTask
from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest


class DefaultFrameAllocator:

    def __init__(self, registry: FleetRegistry) -> None:
        self._registry = registry

    def allocate_initial(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        machines: list[Machine],
    ) -> list[PlannedTask]:
        enabled = [m for m in machines if self._registry.is_enabled(m.machine_type)]
        if not enabled:
            enabled = machines

        tasks = distribute_frames(
            total_frames=total_frames,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            machines=enabled,
            min_frames_fn=lambda m: self._registry.min_frames_per_instance(m.machine_type),
        )

        tasks = expand_serverless_assignments(
            tasks,
            workers_per_endpoint_fn=self._registry.workers_per_endpoint,
            min_frames_fn=self._registry.min_frames_per_instance,
        )

        for i, t in enumerate(tasks):
            tasks[i] = PlannedTask(
                machine_id=t.machine_id,
                machine_type=t.machine_type,
                gpu_model=t.gpu_model,
                gpu_vram_gb=t.gpu_vram_gb,
                frame_start=t.frame_start,
                frame_end=t.frame_end,
                frame_step=t.frame_step,
                total_frames=t.total_frames,
                power_score=t.power_score,
                chunk_index=i,
            )

        return tasks

    def allocate_retry(
        self,
        chunk_request: ChunkRequest,
        machines: list[Machine],
    ) -> PlannedTask | None:
        eligible = [
            m for m in machines
            if m.id not in chunk_request.excluded_machine_ids
        ]
        if not eligible:
            return None
        machine = max(eligible, key=compute_power_score)
        return PlannedTask(
            machine_id=machine.id,
            machine_type=machine.machine_type,
            gpu_model=machine.gpu_model,
            gpu_vram_gb=machine.gpu_vram_gb,
            frame_start=chunk_request.frame_start,
            frame_end=chunk_request.frame_end,
            frame_step=chunk_request.frame_step,
            total_frames=chunk_request.total_frames,
            power_score=compute_power_score(machine),
            chunk_index=chunk_request.chunk_index,
            attempt=chunk_request.attempt,
        )
