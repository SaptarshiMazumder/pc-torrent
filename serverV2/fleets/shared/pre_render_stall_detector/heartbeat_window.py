"""HeartbeatWindow — read-only sliding window of recent heartbeat samples.

Constructed from raw dicts returned by ``HeartbeatRepository.get_recent``.
Newest sample first.  All helpers are pure reads — no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Heartbeat:
    ts: float                  # unix seconds, server-side write time
    phase: str | None
    cpu_percent: float | None
    rss_bytes: int | None
    bytes_progressed: int | None
    total_bytes: int | None


class HeartbeatWindow:

    def __init__(self, samples: list[Heartbeat]) -> None:
        # Caller is responsible for ordering: newest first.  The repo's
        # ``get_recent`` returns the Redis LIST in LPUSH order which is
        # newest-first, so the natural shape lines up.
        self._samples = samples

    @classmethod
    def from_raw(cls, rows: list[dict]) -> "HeartbeatWindow":
        samples: list[Heartbeat] = []
        for row in rows:
            ts = row.get("ts")
            if ts is None:
                continue
            samples.append(Heartbeat(
                ts=float(ts),
                phase=row.get("phase"),
                cpu_percent=_as_float(row.get("cpu_percent")),
                rss_bytes=_as_int(row.get("rss_bytes")),
                bytes_progressed=_as_int(row.get("bytes_progressed")),
                total_bytes=_as_int(row.get("total_bytes")),
            ))
        return cls(samples)

    @property
    def is_empty(self) -> bool:
        return not self._samples

    @property
    def latest(self) -> Heartbeat | None:
        return self._samples[0] if self._samples else None

    @property
    def samples(self) -> list[Heartbeat]:
        return self._samples

    def within_last(self, sec: float) -> list[Heartbeat]:
        """Samples whose ts is within ``sec`` seconds of the latest sample.
        Returns newest-first.  Empty list if window is empty."""
        if not self._samples:
            return []
        cutoff = self._samples[0].ts - sec
        return [s for s in self._samples if s.ts >= cutoff]

    def oldest_in_phase(self, phase: str) -> Heartbeat | None:
        """Earliest contiguous-phase sample.  Walks newest→oldest, returns
        the oldest sample whose phase matches, breaking on the first
        non-matching sample (so a brief earlier visit to ``phase`` doesn't
        confuse the elapsed-in-phase calculation)."""
        oldest = None
        for s in self._samples:
            if s.phase != phase:
                if oldest is None:
                    continue  # haven't entered the phase yet, keep looking
                break          # left the phase going backwards — stop
            oldest = s
        return oldest


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
