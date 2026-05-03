"""Heaviness-band lookup — maps blend file size to a VRAM floor and a
recommended frames-per-machine.

Shared by ``FastRenderAllocationStrategy`` and ``EconomyAllocationStrategy``
(and any future cost-aware allocator).  Both strategies use the same
table so the VRAM-feasibility filter is consistent across tiers.

Public surface:
    band_for(file_size_bytes) -> (vram_floor_gb, frames_per_machine)
    vram_floor_for(file_size_bytes) -> vram_floor_gb
    frames_per_machine_for(file_size_bytes) -> frames_per_machine
"""

from __future__ import annotations

import math


_GB = 1024 * 1024 * 1024

# (max_size_bytes_exclusive, vram_floor_gb, frames_per_machine)
_HEAVINESS_BANDS: tuple[tuple[float, int, int], ...] = (
    (500 * 1024 * 1024,  8,  8),    # light:   < 500 MB
    (2 * _GB,            16, 5),    # medium:  < 2 GB
    (5 * _GB,            24, 3),    # heavy:   < 5 GB
    (math.inf,           48, 2),    # ultra:   >= 5 GB
)

# When file size is unknown, treat as medium.
_DEFAULT_VRAM_FLOOR = 16
_DEFAULT_FRAMES_PER_MACHINE = 5


def band_for(file_size_bytes: int | None) -> tuple[int, int]:
    """Return ``(vram_floor_gb, frames_per_machine)`` for the band that
    matches ``file_size_bytes``.  ``None`` -> defaults (medium-band-ish).
    """
    if file_size_bytes is None or file_size_bytes < 0:
        return _DEFAULT_VRAM_FLOOR, _DEFAULT_FRAMES_PER_MACHINE
    for max_size, vram, frames in _HEAVINESS_BANDS:
        if file_size_bytes < max_size:
            return vram, frames
    # Unreachable thanks to math.inf sentinel, but be safe.
    last = _HEAVINESS_BANDS[-1]
    return last[1], last[2]


def vram_floor_for(file_size_bytes: int | None) -> int:
    """Just the VRAM floor.  For strategies that don't size chunks by
    file weight (e.g. Economy uses a fixed max-machines cap)."""
    return band_for(file_size_bytes)[0]


def frames_per_machine_for(file_size_bytes: int | None) -> int:
    """Just the frames-per-machine target.  For FastRender's chunk-count
    derivation."""
    return band_for(file_size_bytes)[1]
