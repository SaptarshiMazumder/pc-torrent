"""LivenessCheck — heartbeat-dead + frame-progress staleness detection.

Owns two related but distinct invariants:

* **Heartbeat dead** — the worker process stopped calling home.  Uses
  the Redis TTL with a configurable grace window before a missing
  ping counts.
* **Stale** — the worker is pinging but no new frame has uploaded for
  ``stale_sec``.  Guarded by a "never rendered anything yet" short-
  circuit so healthy scene-prep is not mistaken for a stall.

Thresholds (``stale_sec`` / ``heartbeat_grace_sec``) are read from the
job's ``allowed_stall_times`` column at evaluate time, not held at
construction.  This keeps the singleton stateless across config
changes — every job gets the deadline that was stamped on its row at
dispatch.

The grace anchor can be set at construction (Modal — worker is
assumed alive from the moment we start the monitor) or deferred via
:meth:`mark_active` (Vast — worker can't heartbeat until the container
actually reaches "running", which the monitor observes separately).

One instance per monitored job.  Holds only the state needed to
reason about activity over time (``_last_rendered_frames``,
timestamps).
"""

from __future__ import annotations

import time

from serverV2.repositories.heartbeat_repository import HeartbeatRepository


class LivenessCheck:

    def __init__(
        self,
        *,
        heartbeat_repo: HeartbeatRepository,
        defer_activation: bool = False,
    ) -> None:
        self._heartbeats = heartbeat_repo

        self._grace_anchor: float | None = (
            None if defer_activation else time.monotonic()
        )
        self._last_rendered_frames: int | None = None
        self._last_frame_change_at = time.monotonic()

    # ---- heartbeat ----

    def mark_active(self) -> None:
        """Reset the grace-period anchor.  Call when the monitored unit
        actually starts (e.g. Vast container reached "running")."""
        self._grace_anchor = time.monotonic()

    def heartbeat_dead(self, job_id: str, *, grace_sec: float) -> bool:
        if self._grace_anchor is None:
            return False
        if time.monotonic() - self._grace_anchor < grace_sec:
            return False
        alive = self._heartbeats.is_alive(job_id)
        if alive is None:
            return False
        return not alive

    # ---- staleness ----

    def note_activity(self, rendered_count: int) -> None:
        """Call on every tick with the latest rendered count."""
        if rendered_count != self._last_rendered_frames:
            self._last_rendered_frames = rendered_count
            self._last_frame_change_at = time.monotonic()

    def is_stale(self, *, stale_sec: float) -> bool:
        # Never rendered anything yet — scene prep is legitimate.
        if self._last_rendered_frames in (None, 0):
            return False
        return (time.monotonic() - self._last_frame_change_at) > stale_sec
