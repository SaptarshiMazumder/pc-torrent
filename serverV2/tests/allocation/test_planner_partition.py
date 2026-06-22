"""Phase-partition invariant guard for AllocationPlanner.

Frame accounting downstream of the planner assumes every chunk it emits
lands on the global frame-step *phase* and that the chunks exactly tile
the intended frame set — no gaps, no overlaps, nothing off-phase:

  * ``OutputFrameRepository.count_in_range`` counts uploaded frames on
    each chunk's phase (``(frame_number - start) %% step = 0``).
  * ``ChunkProgressService`` diffs each chunk's expected stepped range
    against the frames actually uploaded.

The planner is correct today — ``_distribute_time_balanced`` walks the
range in step multiples, so chunk starts can't drift off-phase — but that
correctness is load-bearing.  This test pins the invariant so a future
planner refactor can't silently reintroduce an off-phase boundary (e.g.
a chunk starting at frame 6 when the phase is 1,4,7,10), which would make
completion under-count and frames go missing.

Resources are abundant community machines with ``available_seconds=None``
so the time-clamp never drops frames — the planner must place every
frame, letting us assert a *complete* partition.
"""

from __future__ import annotations

import pytest

from serverV2.allocation.allocation_strategies.allocation_planner import (
    AllocationPlanner,
)
from serverV2.core.models import AvailableResources
from serverV2.tests.allocation.planner_scenarios import (
    LIGHT_HEAVINESS,
    StubRegistry,
    _community,
    _default_startup_buffer,
    _default_vram_boost,
    _default_weights,
)


def _abundant_community(n: int) -> AvailableResources:
    # Distinct speeds -> distinct scores (no sort-tie ambiguity); vram well
    # above the LIGHT requirement; available_seconds=None -> no time-clamp,
    # so every frame is placed and the chunks form a complete partition.
    return AvailableResources(
        community_machines=[
            _community(id_=f"pc-{i:02d}", gpu="RTX 4090", vram=24.0, speed=2.0 - i * 0.05)
            for i in range(n)
        ],
        serverless_capabilities=[],
        serverless_in_flight={},
    )


def _stepped(start: int, end: int, step: int) -> set[int]:
    return set(range(start, end + 1, step))


# (frame_start, frame_end, frame_step, machine_count)
_PARTITION_CASES = [
    (1, 100, 1, 6),   # contiguous, multi-chunk
    (1, 20, 1, 4),
    (1, 3, 1, 4),     # tiny -> single chunk
    (5, 5, 1, 4),     # single frame
    (1, 10, 3, 4),    # stepped, end ON-phase {1,4,7,10}
    (1, 9, 3, 4),     # stepped, end OFF-phase {1,4,7}
    (1, 100, 3, 6),   # stepped, multi-chunk, end on-phase
    (1, 98, 3, 6),    # stepped, multi-chunk, end OFF-phase
    (10, 40, 2, 5),   # stepped from a non-1 start
    (2, 47, 5, 5),
]


@pytest.mark.parametrize("frame_start,frame_end,frame_step,machines", _PARTITION_CASES)
def test_planner_chunks_partition_frame_set(
    frame_start: int, frame_end: int, frame_step: int, machines: int,
) -> None:
    expected = _stepped(frame_start, frame_end, frame_step)
    total_frames = len(expected)

    planner = AllocationPlanner(registry=StubRegistry())
    tasks = planner.plan_initial(
        frame_start=frame_start,
        frame_end=frame_end,
        frame_step=frame_step,
        total_frames=total_frames,
        resources=_abundant_community(machines),
        weights=_default_weights(),
        startup_buffer_sec=_default_startup_buffer(),
        vram_fleet_boost=_default_vram_boost(),
        engine="cycles",
        heaviness=LIGHT_HEAVINESS,
    )

    assert tasks, "planner returned no chunks for a feasible render"

    seen: set[int] = set()
    reported_total = 0
    for t in tasks:
        # The frames this chunk will actually render = the worker's
        # range(frame_start, frame_end+1, frame_step) for the chunk.
        chunk_frames = _stepped(t.frame_start, t.frame_end, t.frame_step)

        # 1. same step + chunk start sits on the global phase
        assert t.frame_step == frame_step
        assert (t.frame_start - frame_start) % frame_step == 0, (
            f"chunk start {t.frame_start} is off the step-{frame_step} phase"
        )
        # 2. wholly within the intended set
        assert chunk_frames <= expected, (
            f"chunk {t.frame_start}-{t.frame_end} renders frames outside the render"
        )
        # 3. disjoint from every earlier chunk (no double-rendered frame)
        assert seen.isdisjoint(chunk_frames), (
            f"chunk {t.frame_start}-{t.frame_end} overlaps an earlier chunk"
        )
        # 4. the chunk's declared count matches the frames it renders
        assert t.total_frames == len(chunk_frames), (
            f"chunk {t.frame_start}-{t.frame_end} total_frames={t.total_frames} "
            f"but renders {len(chunk_frames)} frames"
        )

        seen |= chunk_frames
        reported_total += t.total_frames

    # 5. complete tiling — every intended frame covered exactly once
    assert seen == expected, (
        "chunks do not exactly tile the frame set (gap or off-phase boundary)"
    )
    assert reported_total == total_frames
