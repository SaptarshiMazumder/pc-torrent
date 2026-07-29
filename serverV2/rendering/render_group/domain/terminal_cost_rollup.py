"""Domain rule: total actual cost of a terminal render group.

Pure summation over the group's completed chunks.  When a group reaches a
terminal state, the reconcile use-case stamps a frozen ``terminal_snapshot``
so the list endpoint can serve the group forever without re-reading its
children.  The cost figure on that snapshot is what this computes.

Lives in the domain because it is a business calculation, not I/O -- the
use-case gathers the per-chunk costs through a port and hands them here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalCost:
    total_cost_usd: float
    total_rendered_frames: int


def roll_up_terminal_cost(
    *, chunk_costs_usd: list[float], total_rendered_frames: int
) -> TerminalCost:
    raise NotImplementedError
