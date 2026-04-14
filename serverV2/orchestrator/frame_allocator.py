"""FrameAllocator — wraps pure allocation logic + fleet registry lookups.

Single responsibility: turn a frame range + machines into PlannedTasks.
"""

from __future__ import annotations

from serverV2.allocation.frame_distributor import distribute_frames
from serverV2.allocation.serverless_expander import expand_serverless_assignments
from serverV2.core.models import Machine, PlannedTask
from serverV2.fleets.registry import FleetRegistry


class FrameAllocator:

    def __init__(self, registry: FleetRegistry) -> None:
        self._registry = registry

    def allocate(
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
