"""BackendClient — every HTTP call the worker makes to the render backend.

All endpoints are job-scoped, so the client is constructed with a
``(backend_url, job_id)`` pair and callers do not need to re-pass them.

Response-handling policy across all status / register-outputs /
request-upload-urls calls:

* HTTP 4xx/5xx → exception, retried up to deadline.
* Network timeout → exception, retried up to deadline.
* HTTP 200 with body ``{"success": false, ...}`` for terminal status
  writes (done/failed/cancelled): no-op success — server has already
  reached the terminal state via another path (B3's success_notifier
  from register-outputs, monitor force-cancel, etc.).  The worker's
  assertion is irrelevant once the row is terminal.  Stop retrying.
* HTTP 200 with ``success=false`` for non-terminal payloads: real
  rejection — raise.

The endpoints register-outputs and request-upload-urls are idempotent
server-side (merge_output_files dedupes; presigned URL minting is
pure read), so retry-on-timeout is safe and prevents misclassifying
a slow-but-successful response as a failed call.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger(__name__)


_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


class BackendClient:

    _STATUS_RETRY_DEADLINE_SEC = 600
    _UPLOAD_RETRY_DEADLINE_SEC = 180

    def __init__(
        self,
        backend_url: str,
        job_id: str,
        terminal_event: threading.Event | None = None,
    ) -> None:
        self._backend_url = backend_url.rstrip("/")
        self._job_id = job_id
        # Set when the orchestrator returns 410 on push_progress /
        # request_upload_urls / register_outputs.  Shared with
        # HeartbeatSender so any of them can signal terminal; the
        # handler's main loop reads it between subprocess progress
        # lines.
        self._terminal_event = terminal_event

    def _signal_terminal(self, source: str) -> None:
        if self._terminal_event is not None and not self._terminal_event.is_set():
            log.warning(
                "%s returned 410; orchestrator considers the job "
                "terminal -- signalling worker to exit",
                source,
            )
            self._terminal_event.set()

    # ------------------------------------------------------------------
    # Duplicate-start guard (PUT /jobs/{id}/worker-start)
    # ------------------------------------------------------------------

    def try_worker_start(self) -> bool:
        """Return True if this worker is the first to start for this job.
        Return False if another container already claimed — caller aborts.

        Fail-open on network errors: if the backend is unreachable, return
        True so a transient outage doesn't block legitimate renders.  The
        guard re-engages as soon as the backend is reachable again.
        """
        try:
            resp = requests.put(
                f"{self._backend_url}/jobs/{self._job_id}/worker-start",
                timeout=30,
            )
        except Exception as exc:
            log.warning(
                f"worker-start call failed for {self._job_id} "
                f"(failing open — cannot detect duplicate starts): {exc}"
            )
            return True
        if resp.status_code == 409:
            return False
        if 200 <= resp.status_code < 300:
            return True
        log.warning(
            f"worker-start returned HTTP {resp.status_code} "
            f"(failing open): {resp.text[:200]}"
        )
        return True

    # ------------------------------------------------------------------
    # Status updates (PUT /jobs/{id}/status)
    # ------------------------------------------------------------------

    def mark_running(self) -> None:
        """Fire-and-forget: tell the backend this job is running."""
        try:
            requests.put(
                f"{self._backend_url}/jobs/{self._job_id}/status",
                json={"status": "running"},
                timeout=30,
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
        delay = 5
        last_exc: Exception | None = None
        start = time.monotonic()
        attempt = 0
        target = str(payload.get("status") or "")
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
                    reason = str(body.get("reason") or "").strip()
                    if target in _TERMINAL_STATUSES:
                        # Server already moved this job to a terminal state
                        # via another path (success_notifier from register-
                        # outputs, monitor force-cancel, our earlier retry
                        # that succeeded server-side but lost the response).
                        # The job is where we wanted it.  Stop retrying.
                        log.info(
                            f"Status update for job {self._job_id} "
                            f"({target}) accepted as no-op: {reason}"
                        )
                        return
                    raise RuntimeError(
                        f"Backend rejected status update for job "
                        f"{self._job_id}: {reason or 'no reason given'}"
                    )
                return
            except Exception as exc:
                last_exc = exc
                elapsed = time.monotonic() - start
                log.warning(
                    f"Status update attempt {attempt} failed "
                    f"({elapsed:.0f}s elapsed): {exc}"
                )
                if elapsed + delay >= deadline_sec:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 30)
        if last_exc is not None:
            raise last_exc

    # ------------------------------------------------------------------
    # Progress + Heartbeat (fire-and-forget)
    # ------------------------------------------------------------------

    def push_progress(self, rendered_frames: int, total_frames: int) -> None:
        try:
            requests.put(
                f"{self._backend_url}/jobs/{self._job_id}/progress",
                json={"rendered_frames": rendered_frames, "total_frames": total_frames},
                timeout=30,
            )
        except Exception as exc:
            log.warning(f"Failed to push progress: {exc}")

    def heartbeat(self, phase: str) -> None:
        """Minimal heartbeat — phase only.  HeartbeatSender uses its own
        full-payload PUT path; this method exists for callers that just
        want a one-shot ping (modal-side ModalHeartbeat legacy)."""
        try:
            requests.put(
                f"{self._backend_url}/jobs/{self._job_id}/heartbeat",
                json={"phase": phase},
                timeout=30,
            )
        except Exception as exc:
            log.warning(f"Heartbeat failed for job {self._job_id}: {exc}")

    # ------------------------------------------------------------------
    # Cancel-status poll (community workers; cloud workers can also use
    # it to detect server-side cancel mid-render).
    # ------------------------------------------------------------------

    def poll_cancel_status(self) -> bool:
        """Return True if the server has marked this job cancelled or
        failed.  Network errors → False (fail-open: don't abort a healthy
        render because of a momentary blip)."""
        try:
            resp = requests.get(
                f"{self._backend_url}/jobs/{self._job_id}/cancel-status",
                timeout=30,
            )
            resp.raise_for_status()
            return bool(resp.json().get("cancelled"))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Upload flow: presigned URL request -> direct R2 PUT -> register.
    # Both endpoints retry on transient failures because they're
    # idempotent server-side.
    # ------------------------------------------------------------------

    def request_upload_urls(self, filenames: list[str]) -> dict[str, str]:
        """Return ``{filename: presigned_put_url}`` for the given filenames."""
        resp = self._post_with_retry(
            f"{self._backend_url}/jobs/{self._job_id}/request-upload-urls",
            json={"filenames": filenames},
            deadline_sec=self._UPLOAD_RETRY_DEADLINE_SEC,
        )
        return resp.json().get("urls", {}) or {}

    def register_outputs(self, files: list[tuple[str, bool]]) -> None:
        """Register uploaded ``(filename, is_primary)`` pairs with the server.
        Retries on transient errors — the endpoint is idempotent (dedupes by
        (group, filename) and keeps ``is_primary`` monotonic), so a slow-but-
        successful response classified as a timeout is safe to retry instead
        of bubbling to mark_failed."""
        self._post_with_retry(
            f"{self._backend_url}/jobs/{self._job_id}/register-outputs",
            json={"files": [
                {"filename": name, "is_primary": is_primary}
                for name, is_primary in files
            ]},
            deadline_sec=self._UPLOAD_RETRY_DEADLINE_SEC,
        )

    def _post_with_retry(
        self, url: str, *, json: dict, deadline_sec: float, timeout: float = 30,
    ) -> requests.Response:
        delay = 5
        last_exc: Exception | None = None
        start = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = requests.post(url, json=json, timeout=timeout)
                resp.raise_for_status()
                return resp
            except Exception as exc:
                last_exc = exc
                elapsed = time.monotonic() - start
                log.warning(
                    f"POST {url} attempt {attempt} failed "
                    f"({elapsed:.0f}s elapsed): {exc}"
                )
                if elapsed + delay >= deadline_sec:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 30)
        assert last_exc is not None
        raise last_exc
