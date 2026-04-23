"""ModalHeartbeat — periodic ping so the backend can detect dead containers
quickly instead of waiting for the stale-frames timeout."""

from __future__ import annotations

import logging
import threading

from modal_worker.backend_client import BackendClient

log = logging.getLogger(__name__)


class ModalHeartbeat:

    INTERVAL_SEC = 10

    def __init__(self, client: BackendClient, job_id: str) -> None:
        self._client = client
        self._job_id = job_id
        self._phase: str = "starting"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, phase: str = "starting") -> None:
        self._phase = phase
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"modal-hb-{self._job_id[:8]}",
        )
        self._thread.start()

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.INTERVAL_SEC):
            self._client.heartbeat(self._phase)
