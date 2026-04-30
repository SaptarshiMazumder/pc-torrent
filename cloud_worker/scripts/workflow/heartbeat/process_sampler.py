"""ProcessSampler -- psutil wrapper for current process CPU% and RSS.

Used by the heartbeat sender to emit activity signals.  Server-side
stall detection compares successive samples: low CPU + flat RSS over
a sliding window means the worker is wedged regardless of what phase
the heartbeat says it's in.
"""

from __future__ import annotations

import os

import psutil


class ProcessSampler:

    def __init__(self) -> None:
        self._proc = psutil.Process(os.getpid())
        # Prime the CPU% baseline so the first real call returns a real
        # value instead of the all-zero first-call result.
        self._proc.cpu_percent(interval=None)

    def sample(self) -> dict[str, float | int]:
        """Returns {cpu_percent, rss_bytes}.

        cpu_percent is averaged over the interval since the last call,
        per psutil convention -- meaningful only when called periodically.
        """
        return {
            "cpu_percent": float(self._proc.cpu_percent(interval=None)),
            "rss_bytes": int(self._proc.memory_info().rss),
        }
