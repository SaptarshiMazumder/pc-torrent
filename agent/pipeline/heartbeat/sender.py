"""JobHeartbeatSender -- per-job heartbeat thread for the agent.

The agent already sends machine-level heartbeats to /machines/{mid}/
heartbeat in start_heartbeat_loop.  This sender adds the parallel
per-job heartbeat to /jobs/{job_id}/heartbeat carrying phase +
bytes_progressed -- the signals the server's stall detector consumes.

CPU/RSS sampling is intentionally NOT included.  The agent's own
process activity isn't a useful proxy for the rendering Docker
container's activity (the render runs OUT-OF-PROCESS from the agent),
so sampling the agent process would mislead the detector.  CPU/RSS
for community is a follow-up that would need docker-stats sampling.
"""

from __future__ import annotations

import threading

import requests

from .bytes_progress import BytesProgress
from .phase_tracker import PhaseTracker


class JobHeartbeatSender:

    def __init__(
        self,
        *,
        backend_url: str,
        job_id: str,
        phase_tracker: PhaseTracker,
        bytes_progress: BytesProgress | None,
        interval_sec: float,
        on_error,
    ) -> None:
        """on_error(exc) is called per failed push.  Caller decides
        whether/how to log -- agent uses _log with sidecar IPC, which
        this module shouldn't know about."""
        self._url = f"{backend_url.rstrip('/')}/jobs/{job_id}/heartbeat"
        self._phase = phase_tracker
        self._bytes = bytes_progress
        self._interval = interval_sec
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="job-heartbeat",
        )
        self._thread.start()
        self._push()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def set_phase(self, phase: str) -> None:
        self._phase.set(phase)
        self._push()

    def _payload(self) -> dict:
        payload: dict = {"phase": self._phase.get()}
        if self._bytes is not None:
            payload["bytes_progressed"] = self._bytes.get()
        return payload

    def _push(self) -> None:
        try:
            requests.put(
                self._url, json=self._payload(), timeout=15,
            ).raise_for_status()
        except Exception as exc:
            self._on_error(exc)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._push()
            self._stop.wait(self._interval)
