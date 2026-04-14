"""Pure functions: cap how many machines / workers the frame budget can justify."""

from __future__ import annotations

from typing import Callable

from serverV2.allocation.power_scorer import compute_power_score
from serverV2.core.models import Machine

DEFAULT_MIN_FRAMES_PER_WORKER = 2


def max_workers_for_frame_budget(
    total_frames: int,
    requested_workers: int,
    min_frames: int = DEFAULT_MIN_FRAMES_PER_WORKER,
) -> int:
    if requested_workers <= 1 or total_frames <= 0:
        return 1
    return max(1, min(requested_workers, total_frames // min_frames))


def limit_machines_for_frame_budget(
    machines: list[Machine],
    total_frames: int,
    min_frames_fn: Callable[[Machine], int] | None = None,
) -> list[Machine]:
    if not machines:
        return []

    ranked = sorted(machines, key=compute_power_score, reverse=True)
    kept: list[Machine] = []
    remaining = total_frames

    for machine in ranked:
        mf = min_frames_fn(machine) if min_frames_fn else DEFAULT_MIN_FRAMES_PER_WORKER
        if remaining >= mf:
            kept.append(machine)
            remaining -= mf

    return kept if kept else ranked[:1]
