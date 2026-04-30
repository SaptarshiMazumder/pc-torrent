"""PhaseTracker -- thread-safe phase state for the worker's heartbeat."""

from __future__ import annotations

import threading


class PhaseTracker:
    """Tracks which workflow phase the worker is in.

    Phases (in order):
        "initializing" -> "download" -> "extract" -> "loading"
            -> "rendering" -> "uploading"

    Server-side stall detection uses the phase to decide which rule
    applies (e.g., download-bytes-stall only matters in "download";
    rendering uses the existing frame-count is_stale check).
    """

    _ALLOWED = frozenset({
        "initializing", "download", "extract",
        "loading", "rendering", "uploading",
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
