"""Pure function: score a machine's compute power for proportional frame splitting."""

from __future__ import annotations

from serverV2.core.models import Machine


def compute_power_score(machine: Machine) -> float:
    base = (machine.gpu_vram_gb * 2) + (machine.cpu_cores * 0.5) + (machine.ram_gb * 0.2)
    return base * machine.render_speed
