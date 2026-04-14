"""Pure function: fan out serverless assignments into parallel sub-tasks."""

from __future__ import annotations

from typing import Callable

from serverV2.allocation.budget_limiter import (
    DEFAULT_MIN_FRAMES_PER_WORKER,
    max_workers_for_frame_budget,
)
from serverV2.core.enums import SERVERLESS_TYPE_VALUES
from serverV2.core.models import PlannedTask


def expand_serverless_assignments(
    tasks: list[PlannedTask],
    workers_per_endpoint_fn: Callable[[str], int],
    min_frames_fn: Callable[[str], int] | None = None,
) -> list[PlannedTask]:
    """Split each serverless task into multiple parallel sub-tasks.

    Parameters
    ----------
    workers_per_endpoint_fn:
        ``(machine_type: str) -> int``
    min_frames_fn:
        ``(machine_type: str) -> int``
    """
    expanded: list[PlannedTask] = []

    for task in tasks:
        if task.machine_type not in SERVERLESS_TYPE_VALUES:
            expanded.append(task)
            continue

        effective_workers = workers_per_endpoint_fn(task.machine_type)
        if effective_workers <= 1 or task.total_frames <= 1:
            expanded.append(task)
            continue

        mf = min_frames_fn(task.machine_type) if min_frames_fn else DEFAULT_MIN_FRAMES_PER_WORKER
        worker_count = max_workers_for_frame_budget(task.total_frames, effective_workers, mf)
        if worker_count <= 1:
            expanded.append(task)
            continue

        frames_per_worker = max(1, task.total_frames // worker_count)
        current = task.frame_start

        for w in range(worker_count):
            if current > task.frame_end:
                break
            sub_end = (
                task.frame_end
                if w == worker_count - 1
                else min(current + (frames_per_worker - 1) * task.frame_step, task.frame_end)
            )
            sub_total = (
                ((sub_end - current) // task.frame_step) + 1
                if sub_end >= current
                else 0
            )
            if sub_total <= 0:
                break
            expanded.append(PlannedTask(
                machine_id=task.machine_id,
                machine_type=task.machine_type,
                gpu_model=task.gpu_model,
                gpu_vram_gb=task.gpu_vram_gb,
                frame_start=current,
                frame_end=sub_end,
                frame_step=task.frame_step,
                total_frames=sub_total,
                power_score=task.power_score,
            ))
            current = sub_end + task.frame_step

    return expanded
