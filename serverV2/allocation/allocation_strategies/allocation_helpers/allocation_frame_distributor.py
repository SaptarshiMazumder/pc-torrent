"""Pure function: split a frame range across targets proportionally to power scores.

Returns a list of :class:`FrameShare` — each share carries the target it
was assigned to plus the frame range and chunk total.  The strategy maps
shares to :class:`PlannedTask` with the right fleet discriminant; this
module stays type-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_budget_limiter import limit_machines_for_frame_budget
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_power_scorer import compute_power_score


@dataclass(frozen=True)
class FrameShare:
    target: Any                 # CommunityMachine or FleetCapability
    frame_start: int
    frame_end: int
    total_frames: int
    score: float


def distribute_frames(
    *,
    total_frames: int,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    targets: list[Any],
    min_frames_fn: Callable[[Any], int] | None = None,
) -> list[FrameShare]:
    targets = limit_machines_for_frame_budget(targets, total_frames, min_frames_fn)
    if not targets:
        return []

    scores = [(t, compute_power_score(t)) for t in targets]
    total_score = sum(s for _, s in scores)
    if total_score <= 0:
        total_score = float(len(targets))
        scores = [(t, 1.0) for t in targets]

    shares: list[FrameShare] = []
    current_frame = frame_start

    for i, (target, score) in enumerate(scores):
        if i == len(scores) - 1:
            chunk_end = frame_end
        else:
            share_fraction = score / total_score
            chunk_frames = max(1, round(total_frames * share_fraction))
            chunk_end = min(current_frame + (chunk_frames - 1) * frame_step, frame_end)

        chunk_total = (
            ((chunk_end - current_frame) // frame_step) + 1
            if chunk_end >= current_frame
            else 0
        )
        shares.append(FrameShare(
            target=target,
            frame_start=current_frame,
            frame_end=chunk_end,
            total_frames=chunk_total,
            score=round(score, 1),
        ))
        current_frame = chunk_end + frame_step

    return shares
