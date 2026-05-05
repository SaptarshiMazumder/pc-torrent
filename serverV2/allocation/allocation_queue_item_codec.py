"""AllocationQueueItemCodec -- convert between ``PlannedTask`` and
``AllocationQueueItem``.

One responsibility: shape translation across the dispatch_queue
boundary.  Enqueue path needs PlannedTask -> AllocationQueueItem;
dispatch path needs AllocationQueueItem -> PlannedTask.  No I/O,
no logic beyond field copying.

Carries the per-chunk estimate fields (price_per_hour and the four
``estimated_*`` numbers) through the queue so the dispatch tick
processor can stamp them onto the jobs row without re-running the
planner.
"""

from __future__ import annotations

from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationQueueItem,
)
from serverV2.core.models import DispatchContext, PlannedTask


class AllocationQueueItemCodec:

    @staticmethod
    def encode(
        task: PlannedTask,
        context: DispatchContext,
        *,
        job_id: str,
    ) -> AllocationQueueItem:
        return AllocationQueueItem(
            frame_start=task.frame_start,
            frame_end=task.frame_end,
            frame_step=task.frame_step,
            total_frames=task.total_frames,
            fleet=task.fleet,
            label=task.label,
            vram_gb=task.vram_gb,
            render_speed=task.render_speed,
            gpu_type=task.gpu_type,
            machine_id=task.machine_id,
            input_filename=context.input_filename,
            render_overrides_json=context.render_overrides_json,
            max_retries=context.max_retries,
            priority=context.priority,
            attempt=task.attempt,
            chunk_index=task.chunk_index,
            job_id=job_id,
            price_per_hour=task.price_per_hour,
            estimated_seconds=task.estimated_seconds,
            estimated_cost_usd=task.estimated_cost_usd,
            estimated_seconds_per_frame=task.estimated_seconds_per_frame,
            estimated_startup_seconds=task.estimated_startup_seconds,
        )

    @staticmethod
    def decode(item: AllocationQueueItem) -> PlannedTask:
        return PlannedTask(
            fleet=item.fleet,
            machine_id=item.machine_id,
            gpu_type=item.gpu_type,
            label=item.label,
            vram_gb=item.vram_gb,
            render_speed=item.render_speed,
            frame_start=item.frame_start,
            frame_end=item.frame_end,
            frame_step=item.frame_step,
            total_frames=item.total_frames,
            chunk_index=item.chunk_index,
            attempt=item.attempt,
            price_per_hour=item.price_per_hour,
            estimated_seconds=item.estimated_seconds,
            estimated_cost_usd=item.estimated_cost_usd,
            estimated_seconds_per_frame=item.estimated_seconds_per_frame,
            estimated_startup_seconds=item.estimated_startup_seconds,
        )
