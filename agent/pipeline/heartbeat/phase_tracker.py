"""PhaseTracker -- thread-safe phase state for the agent's heartbeat."""

from __future__ import annotations

import threading


class PhaseTracker:
    """Tracks which workflow phase the agent is in.

    Phases (in order):
        "initializing" -> "download" -> "running" -> "uploading"

    The agent has fewer distinct phases than cloud_worker because the
    actual rendering happens inside the Docker container -- from the
    agent's POV, between "download finished" and "render done" it's
    all one opaque "running" phase.  Server-side stall detection
    rules that target download progress (Rule 2 / Rule 3) only fire
    while phase=download.
    """

    _ALLOWED = frozenset({
        "initializing", "download", "running", "uploading",
    })

    def __init__(self, initial: str = "initializing") -> None:
        if initial not in self._ALLOWED:
            raise ValueError(f"Unknown phase: {initial!r}")
        self._phase = initial
        self._lock = threading.Lock()

    def set(self, phase: str) -> None:
        if phase not in self._ALLOWED:
            raise ValueError(f"Unknown phase: {phase!r}")
        with self._lock:
            self._phase = phase

    def get(self) -> str:
        with self._lock:
            return self._phase
