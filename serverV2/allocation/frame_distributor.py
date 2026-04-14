"""Pure function: split a frame range across machines proportionally to power scores."""

from __future__ import annotations

from typing import Callable

from serverV2.allocation.budget_limiter import limit_machines_for_frame_budget
from serverV2.allocation.power_scorer import compute_power_score
from serverV2.core.models import Machine, PlannedTask


def distribute_frames(
    *,
    total_frames: int,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    machines: list[Machine],
    min_frames_fn: Callable[[Machine], int] | None = None,
) -> list[PlannedTask]:
    machines = limit_machines_for_frame_budget(machines, total_frames, min_frames_fn)
    if not machines:
        return []

    scores = [(m, compute_power_score(m)) for m in machines]
    total_score = sum(s for _, s in scores)
    if total_score <= 0:
        total_score = float(len(machines))
        scores = [(m, 1.0) for m in machines]

    tasks: list[PlannedTask] = []
    current_frame = frame_start

    for i, (machine, score) in enumerate(scores):
        if i == len(scores) - 1:
            chunk_end = frame_end
        else:
            share = score / total_score
            chunk_frames = max(1, round(total_frames * share))
            chunk_end = min(current_frame + (chunk_frames - 1) * frame_step, frame_end)

        chunk_total = (
            ((chunk_end - current_frame) // frame_step) + 1
            if chunk_end >= current_frame
            else 0
        )
        tasks.append(PlannedTask(
            machine_id=machine.id,
            machine_type=machine.machine_type,
            gpu_model=machine.gpu_model,
            gpu_vram_gb=machine.gpu_vram_gb,
            frame_start=current_frame,
            frame_end=chunk_end,
            frame_step=frame_step,
            total_frames=chunk_total,
            power_score=round(score, 1),
        ))
        current_frame = chunk_end + frame_step

    return tasks
