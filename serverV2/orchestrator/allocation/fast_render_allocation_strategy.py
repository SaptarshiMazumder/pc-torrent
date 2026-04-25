"""FastRenderAllocationStrategy — heaviness-aware, fan-out-aware allocator.

Selects a *mix* of fleet targets sized to the scene's heaviness and
distributes frames across them weighted by render_speed.  Used for big
or many-frame renders where the Default's "one chunk per eligible
target" is too coarse.

Logic per ``allocate_initial`` call:

  1. Determine heaviness band from ``file_size_bytes``.
  2. Filter eligible targets: drop anything with ``vram_gb < scene_min_vram``.
     Fall back to "anything available" if nothing fits (better to try than
     refuse).
  3. Compute desired machine count: ``ceil(total_frames / target_per_machine)``,
     clamped to ``HARD_CAP``.
  4. Speed-first selection respecting fleet caps: fastest target first,
     take as many slots as fleet headroom allows, then move on.
  5. Distribute frames across the selected mix, weighted by
     ``compute_power_score`` (so faster cards get more frames).
"""

from __future__ import annotations

import math

from serverV2.allocation.frame_distributor import distribute_frames
from serverV2.allocation.power_scorer import compute_power_score
from serverV2.core.models import (
    AvailableResources,
    CommunityMachine,
    PlannedTask,
)
from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest

# How many parallel containers a single render is willing to fan out to.
# Below this we'd be wasting frames on too-few machines; above this the
# coordination overhead and Modal/Vast spin-up tax kicks in.
HARD_CAP = 12

# Heaviness bands and their tuning knobs.
#  * vram_floor_gb     — drop targets below this (won't fit the scene)
#  * frames_per_machine — target chunk size; smaller = more parallelism
_GB = 1024 * 1024 * 1024
_HEAVINESS_BANDS = (
    # (max_size_bytes_exclusive, vram_floor_gb, frames_per_machine)
    (500 * 1024 * 1024,  8,  8),    # light:   < 500 MB
    (2 * _GB,            16, 5),    # medium:  < 2 GB
    (5 * _GB,            24, 3),    # heavy:   < 5 GB
    (math.inf,           48, 2),    # ultra:   ≥ 5 GB
)
# When file size is unknown, treat as medium.
_DEFAULT_VRAM_FLOOR = 16
_DEFAULT_FRAMES_PER_MACHINE = 5


class FastRenderAllocationStrategy:

    def __init__(self, registry: FleetRegistry) -> None:
        self._registry = registry

    # ------------------------------------------------------------------
    # initial allocation
    # ------------------------------------------------------------------

    def allocate_initial(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        file_size_bytes: int | None = None,
    ) -> list[PlannedTask]:
        vram_floor, frames_per_machine = _band_for(file_size_bytes)
        eligible = self._eligible_targets(resources, vram_floor)
        if not eligible:
            # Fall back: everything currently available.  Better to try than
            # refuse — a too-small VRAM card may still complete a smaller
            # chunk if the scene streams.
            eligible = self._eligible_targets(resources, vram_gb_min=0)
        if not eligible:
            return []

        ideal_count = max(1, min(HARD_CAP, math.ceil(total_frames / frames_per_machine)))

        fleet_caps = self._fleet_caps(resources)
        in_flight = dict(resources.serverless_in_flight)

        selected = self._select_mix(eligible, ideal_count, in_flight, fleet_caps)
        if not selected:
            return []

        shares = distribute_frames(
            total_frames=total_frames,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            targets=selected,
            min_frames_fn=self._min_frames_for,
        )

        tasks: list[PlannedTask] = []
        for i, share in enumerate(shares):
            tasks.append(self._share_to_task(
                share, frame_step=frame_step, chunk_index=i, attempt=0,
            ))
        return tasks

    # ------------------------------------------------------------------
    # retry — single chunk, anti-affinity-respecting
    # ------------------------------------------------------------------

    def allocate_retry(
        self,
        chunk_request: ChunkRequest,
        resources: AvailableResources,
    ) -> PlannedTask | None:
        vram_floor, _ = _band_for(chunk_request.file_size_bytes)
        eligible = self._eligible_targets(resources, vram_floor)
        if not eligible:
            eligible = self._eligible_targets(resources, vram_gb_min=0)

        excluded_caps = set(chunk_request.excluded_serverless_capabilities)
        excluded_ids = set(chunk_request.excluded_machine_ids)
        eligible = [
            t for t in eligible
            if (
                t.id not in excluded_ids if isinstance(t, CommunityMachine)
                else (t.fleet, t.gpu_type) not in excluded_caps
            )
        ]
        if not eligible:
            return None

        target = max(eligible, key=compute_power_score)
        return self._target_to_task(
            target,
            frame_start=chunk_request.frame_start,
            frame_end=chunk_request.frame_end,
            frame_step=chunk_request.frame_step,
            total_frames=chunk_request.total_frames,
            chunk_index=chunk_request.chunk_index,
            attempt=chunk_request.attempt,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _eligible_targets(
        self, resources: AvailableResources, vram_gb_min: float,
    ) -> list:
        out: list = []
        for m in resources.community_machines:
            if m.vram_gb >= vram_gb_min:
                out.append(m)
        in_flight = resources.serverless_in_flight
        for cap in resources.serverless_capabilities:
            if not self._registry.is_enabled(cap.fleet):
                continue
            if in_flight.get(cap.fleet, 0) >= cap.fleet_max_parallel:
                continue
            if cap.vram_gb >= vram_gb_min:
                out.append(cap)
        return out

    @staticmethod
    def _fleet_caps(resources: AvailableResources) -> dict[str, int]:
        caps: dict[str, int] = {}
        for cap in resources.serverless_capabilities:
            caps[cap.fleet] = cap.fleet_max_parallel
        return caps

    @staticmethod
    def _select_mix(
        eligible: list,
        ideal_count: int,
        in_flight: dict[str, int],
        fleet_caps: dict[str, int],
    ) -> list:
        """Speed-first selection: take fastest targets up to ``ideal_count``,
        respecting per-fleet headroom (each serverless pick consumes one slot
        of its fleet).  Community machines are taken at most once each."""
        sorted_targets = sorted(eligible, key=compute_power_score, reverse=True)
        selected: list = []
        headroom = {f: cap - in_flight.get(f, 0) for f, cap in fleet_caps.items()}
        community_taken: set[str] = set()

        for target in sorted_targets:
            if len(selected) >= ideal_count:
                break
            if isinstance(target, CommunityMachine):
                if target.id in community_taken:
                    continue
                selected.append(target)
                community_taken.add(target.id)
                continue
            # FleetCapability: take as many as fleet headroom + remaining slots allow
            available = max(0, headroom.get(target.fleet, 0))
            remaining = ideal_count - len(selected)
            take = min(available, remaining)
            for _ in range(take):
                selected.append(target)
            headroom[target.fleet] = headroom.get(target.fleet, 0) - take

        return selected

    def _min_frames_for(self, target) -> int:
        if isinstance(target, CommunityMachine):
            return self._registry.min_frames_per_instance("community")
        return self._registry.min_frames_per_instance(target.fleet)

    def _share_to_task(
        self, share, *, frame_step: int, chunk_index: int, attempt: int,
    ) -> PlannedTask:
        return self._target_to_task(
            share.target,
            frame_start=share.frame_start,
            frame_end=share.frame_end,
            frame_step=frame_step,
            total_frames=share.total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
        )

    @staticmethod
    def _target_to_task(
        target, *,
        frame_start: int, frame_end: int, frame_step: int, total_frames: int,
        chunk_index: int | None, attempt: int,
    ) -> PlannedTask:
        if isinstance(target, CommunityMachine):
            return PlannedTask(
                fleet="community",
                machine_id=target.id,
                gpu_type=None,
                label=target.gpu_model,
                vram_gb=target.vram_gb,
                render_speed=target.render_speed,
                frame_start=frame_start,
                frame_end=frame_end,
                frame_step=frame_step,
                total_frames=total_frames,
                chunk_index=chunk_index,
                attempt=attempt,
            )
        # FleetCapability
        return PlannedTask(
            fleet=target.fleet,
            machine_id=None,
            gpu_type=target.gpu_type,
            label=target.label,
            vram_gb=target.vram_gb,
            render_speed=target.render_speed,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
        )


# ----------------------------------------------------------------------
# heaviness band lookup
# ----------------------------------------------------------------------

def _band_for(file_size_bytes: int | None) -> tuple[float, int]:
    """Return ``(vram_floor_gb, frames_per_machine)`` for the band that
    matches ``file_size_bytes``.  ``None`` → defaults (medium-band-ish)."""
    if file_size_bytes is None or file_size_bytes < 0:
        return _DEFAULT_VRAM_FLOOR, _DEFAULT_FRAMES_PER_MACHINE
    for max_size, vram, frames in _HEAVINESS_BANDS:
        if file_size_bytes < max_size:
            return vram, frames
    # Should be unreachable thanks to the math.inf sentinel, but be safe.
    last_vram, last_frames = _HEAVINESS_BANDS[-1][1], _HEAVINESS_BANDS[-1][2]
    return last_vram, last_frames
