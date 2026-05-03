"""Pure functions: cap how many targets the frame budget can justify.

Duck-typed — accepts ``CommunityMachine``, ``FleetCapability`` or any
duck-equivalent (anything ``compute_power_score`` will accept).
"""

from __future__ import annotations

from typing import Any, Callable

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_power_scorer import compute_power_score

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
    targets: list[Any],
    total_frames: int,
    min_frames_fn: Callable[[Any], int] | None = None,
) -> list[Any]:
    """Drop targets the frame budget cannot keep busy.

    Targets are kept in descending power-score order; we walk the ranked
    list and stop once the running budget drops below the per-target
    minimum.  Always returns at least one target if any were given.
    """
    if not targets:
        return []

    ranked = sorted(targets, key=compute_power_score, reverse=True)
    kept: list[Any] = []
    remaining = total_frames

    for target in ranked:
        mf = min_frames_fn(target) if min_frames_fn else DEFAULT_MIN_FRAMES_PER_WORKER
        if remaining >= mf:
            kept.append(target)
            remaining -= mf

    return kept if kept else ranked[:1]
