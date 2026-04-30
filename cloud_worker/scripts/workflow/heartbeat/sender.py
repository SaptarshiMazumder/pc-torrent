"""HeartbeatSender -- periodic HTTP heartbeat composing phase + activity.

Replaces the inline WorkerHeartbeat class that used to live in handler.py.
Composition over inheritance: takes a PhaseTracker plus optional
ProcessSampler and BytesProgress (either may be None when the caller
doesn't want that signal in the payload).
"""

from __future__ import annotations

import logging
import threading

import requests

from .bytes_progress import BytesProgress
from .phase_tracker import PhaseTracker
from .process_sampler import ProcessSampler

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SEC = 5.0


class HeartbeatSender:

    def __init__(
        self,
        *,
        backend_url: str,
        job_id: str,
        phase_tracker: PhaseTracker,
        process_sampler: ProcessSampler | None = None,
        bytes_progress: BytesProgress | None = None,
        interval_sec: float = _DEFAULT_INTERVAL_SEC,
    ) -> None:
        self._url = f"{backend_url.rstrip('/')}/jobs/{job_id}/heartbeat"
        self._phase = phase_tracker
        self._sampler = process_sampler
        self._bytes = bytes_progress
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="heartbeat",
        )
        self._thread.start()
        # Immediate first beat so the server sees activity ASAP.
        self._push()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def set_phase(self, phase: str) -> None:
        """Convenience pass-through so callers don't need to keep a
        separate reference to the PhaseTracker.  Triggers an immediate
        beat at the new phase rather than waiting for the next tick."""
        self._phase.set(phase)
        self._push()

    def _payload(self) -> dict:
        payload: dict = {"phase": self._phase.get()}
        if self._sampler is not None:
            payload.update(self._sampler.sample())
        if self._bytes is not None:
            payload["bytes_progressed"] = self._bytes.get()
        return payload

    def _push(self) -> None:
        try:
            requests.put(
                self._url, json=self._payload(), timeout=15,
            ).raise_for_status()
        except Exception as e:
            log.warning(f"Heartbeat push failed: {e}")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._push()
            self._stop.wait(self._interval)
