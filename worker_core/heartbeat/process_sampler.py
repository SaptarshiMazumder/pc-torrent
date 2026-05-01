"""ProcessSampler -- psutil wrapper for current process CPU% and RSS.

Used by the heartbeat sender to emit activity signals.  Server-side
stall detection compares successive samples: low CPU + flat RSS over
a sliding window means the worker is wedged regardless of what phase
the heartbeat says it's in.

Walks the process tree (self + all children) when sampling, so on
fleets where the heartbeat thread runs in the orchestrator process
and the actual work runs in a child (Blender subprocess), the sampled
CPU reflects the real activity.  ``include_children=False`` opts out.
"""

from __future__ import annotations

import logging
import os

import psutil

log = logging.getLogger(__name__)


class ProcessSampler:

    def __init__(self, *, include_children: bool = True) -> None:
        self._proc = psutil.Process(os.getpid())
        self._include_children = include_children
        # Prime the CPU% baseline so the first real call returns a real
        # value instead of the all-zero first-call result.
        self._proc.cpu_percent(interval=None)
        if include_children:
            for child in self._safe_children():
                try:
                    child.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

    def sample(self) -> dict[str, float | int]:
        """Returns {cpu_percent, rss_bytes}.

        cpu_percent is averaged over the interval since the last call,
        per psutil convention -- meaningful only when called periodically.
        Children that appeared since last sample contribute 0% on their
        first sample (psutil baseline), so a freshly-spawned Blender
        registers on the second tick.
        """
        cpu = float(self._proc.cpu_percent(interval=None))
        rss = int(self._proc.memory_info().rss)
        if self._include_children:
            for child in self._safe_children():
                try:
                    cpu += float(child.cpu_percent(interval=None))
                    rss += int(child.memory_info().rss)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        return {"cpu_percent": cpu, "rss_bytes": rss}

    def _safe_children(self) -> list[psutil.Process]:
        try:
            return self._proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []
