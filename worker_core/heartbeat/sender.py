"""HeartbeatSender -- periodic HTTP heartbeat composing phase + activity.

Composition over inheritance: takes a PhaseTracker plus optional
ProcessSampler and BytesProgress (either may be None when the caller
doesn't want that signal in the payload).  A fleet that doesn't have
a useful CPU/RSS signal (e.g. the agent runs the actual render in a
child docker container, not in its own process) just omits the
sampler -- the server's stall detector treats missing fields as
no-signal and skips the rule that needs them.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import requests

from worker_core.heartbeat.bytes_progress import BytesProgress
from worker_core.heartbeat.phase_tracker import PhaseTracker
from worker_core.heartbeat.process_sampler import ProcessSampler

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
        on_error: Callable[[Exception], None] | None = None,
        terminal_event: threading.Event | None = None,
    ) -> None:
        # ``on_error(exc)`` lets a caller route push failures through its
        # own logging pipeline (e.g. the agent's sidecar-IPC log bridge)
        # instead of the default module logger.  Defaults to log.warning.
        # ``terminal_event`` is set when the orchestrator returns HTTP
        # 410 Gone -- the job has reached a terminal status server-side
        # (done / failed / cancelled).  The handler's main loop reads
        # this event between subprocess progress lines and exits cleanly
        # instead of waiting for heartbeat-staleness or the worker
        # finishing a render the orchestrator no longer wants.
        self._url = f"{backend_url.rstrip('/')}/jobs/{job_id}/heartbeat"
        self._phase = phase_tracker
        self._sampler = process_sampler
        self._bytes = bytes_progress
        self._interval = interval_sec
        self._on_error = on_error
        self._terminal_event = terminal_event
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
        """Pass-through so callers don't need a separate PhaseTracker
        reference.  Triggers an immediate beat at the new phase rather
        than waiting for the next tick."""
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
            resp = requests.put(self._url, json=self._payload(), timeout=30)
            if resp.status_code == 410:
                # Job terminal -- orchestrator no longer wants this work.
                # Signal and stop heartbeating; main loop reads the event
                # and exits the worker.
                if self._terminal_event is not None and not self._terminal_event.is_set():
                    log.warning(
                        "Heartbeat returned 410; orchestrator considers "
                        "the job terminal -- signalling worker to exit"
                    )
                    self._terminal_event.set()
                self._stop.set()
                return
            resp.raise_for_status()
        except Exception as e:
            if self._on_error is not None:
                self._on_error(e)
            else:
                log.warning(f"Heartbeat push failed: {e}")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._push()
            self._stop.wait(self._interval)
