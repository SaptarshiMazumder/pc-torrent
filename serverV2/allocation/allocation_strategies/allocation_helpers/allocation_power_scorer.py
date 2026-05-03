"""Pure function: score an allocatable target for proportional frame splitting.

Duck-typed.  Accepts any object with ``vram_gb``, ``cpu_cores``, ``ram_gb``
and ``render_speed`` attributes — works for ``CommunityMachine`` and
``FleetCapability`` interchangeably.
"""

from __future__ import annotations


def compute_power_score(target) -> float:
    base = (
        (target.vram_gb * 2)
        + (target.cpu_cores * 0.5)
        + (target.ram_gb * 0.2)
    )
    return base * target.render_speed
