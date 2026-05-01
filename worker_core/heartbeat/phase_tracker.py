"""PhaseTracker -- thread-safe phase state for the worker's heartbeat.

The set of allowed phases differs across fleets (cloud workers see
extract/loading/rendering as separate phases; the agent sees the
container as one opaque "running" phase), so the allowed set is a
constructor argument.  Server-side stall detection rules read the
phase to decide which rule applies.
"""

from __future__ import annotations

import threading


class PhaseTracker:

    def __init__(self, *, allowed: frozenset[str], initial: str) -> None:
        if initial not in allowed:
            raise ValueError(f"Initial phase {initial!r} not in allowed set")
        self._allowed = allowed
        self._phase = initial
        self._lock = threading.Lock()

    def set(self, phase: str) -> None:
        if phase not in self._allowed:
            raise ValueError(
                f"Unknown phase {phase!r} (allowed: {sorted(self._allowed)})"
            )
        with self._lock:
            self._phase = phase

    def get(self) -> str:
        with self._lock:
            return self._phase
