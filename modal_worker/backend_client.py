"""BackendClient — every HTTP call the worker makes to the render backend.

All endpoints are job-scoped, so the client is constructed with a
``(backend_url, job_id)`` pair and callers do not need to re-pass them.

Behaviour preserved 1:1 from the pre-split handler.py:
* ``mark_running`` is fire-and-forget — logs on failure but never raises.
* ``push_progress`` is fire-and-forget — same treatment.
* ``mark_failed`` uses the retry-with-deadline PUT (``_put_status``).
* ``mark_done`` uses the retry-with-deadline PUT; raises on exhausted retries.
* ``request_upload_urls`` and ``register_outputs`` raise on failure — the
  caller (uploader / catch-up path) decides how to react.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class BackendClient:

    _STATUS_RETRY_DEADLINE_SEC = 600

    def __init__(self, backend_url: str, job_id: str) -> None:
        self._backend_url = backend_url.rstrip("/")
        self._job_id = job_id

    # ------------------------------------------------------------------
    # Status updates (PUT /jobs/{id}/status)
    # ------------------------------------------------------------------

    def mark_running(self) -> None:
        """Fire-and-forget: tell the backend this job is running."""
        try:
            requests.put(
                f"{self._backend_url}/jobs/{self._job_id}/status",
                json={"status": "running"},
                timeout=15,
            )
        except Exception as exc:
            log.warning(f"Failed to mark job running: {exc}")

    def mark_done(self, output_files: list[str]) -> None:
        """Retrying PUT: tell the backend the job completed successfully."""
        self._put_status(
            {"status": "done", "output_files": output_files},
            deadline_sec=self._STATUS_RETRY_DEADLINE_SEC,
        )

    def mark_failed(self, error: str) -> None:
        """Retrying PUT: tell the backend the job failed.  Swallows the
        final exception after the deadline — a failure-to-report-failure
        should not itself propagate."""
        try:
            self._put_status(
                {"status": "failed", "error": error},
                deadline_sec=self._STATUS_RETRY_DEADLINE_SEC,
            )
        except Exception as exc:
            log.error(f"Failed to mark job failed after retries: {exc}")

    def _put_status(self, payload: dict, deadline_sec: float) -> None:
        """PUT job status with retries to tolerate ngrok/Cloud Run latency spikes."""
        delay = 5
        last_exc: Exception | None = None
        start = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = requests.put(
                    f"{self._backend_url}/jobs/{self._job_id}/status",
                    json=payload,
                    timeout=60,
                )
                resp.raise_for_status()
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and body.get("success") is False:
                    reason = str(body.get("reason") or "backend rejected status update").strip()
                    raise RuntimeError(
                        f"Backend rejected status update for job {self._job_id}: {reason}"
                    )
                return
            except Exception as exc:
                last_exc = exc
                elapsed = time.monotonic() - start
                log.warning(
                    f"Status update attempt {attempt} failed ({elapsed:.0f}s elapsed): {exc}"
                )
                if time.monotonic() - start + delay >= deadline_sec:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 30)
        if last_exc is not None:
            raise last_exc

    # ------------------------------------------------------------------
    # Progress (PUT /jobs/{id}/progress)
    # ------------------------------------------------------------------

    def push_progress(self, rendered_frames: int, total_frames: int) -> None:
        """Fire-and-forget progress update."""
        try:
            requests.put(
                f"{self._backend_url}/jobs/{self._job_id}/progress",
                json={"rendered_frames": rendered_frames, "total_frames": total_frames},
                timeout=15,
            )
        except Exception as exc:
            log.warning(f"Failed to push progress: {exc}")

    # ------------------------------------------------------------------
    # Heartbeat (PUT /jobs/{id}/heartbeat)
    # ------------------------------------------------------------------

    def heartbeat(self, phase: str) -> None:
        """Fire-and-forget heartbeat ping used by ModalHeartbeat."""
        try:
            requests.put(
                f"{self._backend_url}/jobs/{self._job_id}/heartbeat",
                json={"phase": phase},
                timeout=10,
            )
        except Exception as exc:
            log.warning(f"Heartbeat failed for job {self._job_id}: {exc}")

    # ------------------------------------------------------------------
    # Upload flow: request presigned URLs -> PUT to R2 -> register
    # ------------------------------------------------------------------

    def request_upload_urls(self, filenames: list[str]) -> dict[str, str]:
        """Return ``{filename: presigned_put_url}`` for the given filenames.
        Raises on HTTP error."""
        resp = requests.post(
            f"{self._backend_url}/jobs/{self._job_id}/request-upload-urls",
            json={"filenames": filenames},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("urls", {}) or {}

    def register_outputs(self, filenames: list[str]) -> None:
        """Register uploaded filenames with the server (updates DB
        ``output_files`` list).  Raises on HTTP error."""
        resp = requests.post(
            f"{self._backend_url}/jobs/{self._job_id}/register-outputs",
            json={"filenames": filenames},
            timeout=30,
        )
        resp.raise_for_status()
