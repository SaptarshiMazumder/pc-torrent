"""ModalCallbackHandler — background monitor for Modal jobs.

Detects completion / failure / staleness and reports via injected callables.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from serverV2.config import ModalConfig
from serverV2.fleets.modal.client import ModalClient
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)

_HEARTBEAT_GRACE_SEC = 90
_HEARTBEAT_TIMEOUT_SEC = 45


class ModalCallbackHandler:

    def __init__(
        self,
        config: ModalConfig,
        client: ModalClient,
        job_repo: JobRepository,
        on_failure: Callable[[dict[str, Any], str, str], None],
    ) -> None:
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._on_failure = on_failure

    def start_monitoring(
        self,
        *,
        job_id: str,
        provider_job_id: str,
        machine_id: str,
        blend_url: str,
        render_overrides_b64: str,
        group_id: str,
    ) -> None:
        monitor = _JobMonitor(
            job_id=job_id,
            provider_job_id=provider_job_id,
            machine_id=machine_id,
            group_id=group_id,
            config=self._cfg,
            client=self._client,
            job_repo=self._job_repo,
            on_failure=self._on_failure,
        )
        t = threading.Thread(
            target=monitor.run, daemon=True,
            name=f"modal-mon-{job_id[:8]}",
        )
        t.start()


class _JobMonitor:
    """Encapsulates the state machine for one Modal job monitor loop."""

    def __init__(
        self,
        job_id: str,
        provider_job_id: str,
        machine_id: str,
        group_id: str,
        config: ModalConfig,
        client: ModalClient,
        job_repo: JobRepository,
        on_failure: Callable[[dict[str, Any], str, str], None],
    ) -> None:
        self._job_id = job_id
        self._provider_job_id = provider_job_id
        self._machine_id = machine_id
        self._group_id = group_id
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._on_failure = on_failure

        self._started_at = time.monotonic()
        self._last_activity_at = time.monotonic()
        self._last_activity_marker: tuple[int, int] | None = None

    def run(self) -> None:
        while True:
            time.sleep(self._cfg.monitor_interval_sec)
            try:
                if self._tick():
                    break
            except Exception as exc:
                log.error("Modal poll error for job %s: %s", self._job_id, exc)

    def _tick(self) -> bool:
        job = self._job_repo.get_raw_by_id(self._job_id)
        elapsed = time.monotonic() - self._started_at

        if not job:
            return True

        local_status = str(job.get("status") or "")
        self._update_activity(job)

        if local_status in ("done", "failed", "cancelled"):
            return True

        if self._is_complete(job):
            self._job_repo.mark_done(self._job_id)
            return True

        if local_status == "pending" and elapsed > self._cfg.in_queue_timeout_sec:
            err = f"Modal job stuck in pending for {elapsed:.0f}s"
            if self._is_complete(job):
                self._job_repo.mark_done(self._job_id)
                return True
            self._handle_failure(job, err)
            return True

        if local_status == "running" and self._is_heartbeat_dead(job):
            err = f"Modal job heartbeat dead (no ping for >{_HEARTBEAT_TIMEOUT_SEC}s)"
            if self._is_complete(job):
                self._job_repo.mark_done(self._job_id)
                return True
            self._handle_failure(job, err)
            return True

        if local_status == "running" and self._is_stale():
            err = f"Modal job stale — no new frames for {self._cfg.in_progress_stale_sec / 60:.0f} min"
            if self._is_complete(job):
                self._job_repo.mark_done(self._job_id)
                return True
            self._handle_failure(job, err)
            return True

        return False

    # ---- helpers ----

    def _update_activity(self, job: dict[str, Any]) -> None:
        marker = (job.get("rendered_frames") or 0, self._output_count(job))
        if marker != self._last_activity_marker:
            self._last_activity_marker = marker
            self._last_activity_at = time.monotonic()

    def _is_complete(self, job: dict[str, Any]) -> bool:
        total = job.get("total_frames") or 0
        if total <= 0:
            return False
        rendered = job.get("rendered_frames") or 0
        return rendered >= total or self._output_count(job) >= total

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._last_activity_at) > self._cfg.in_progress_stale_sec

    def _is_heartbeat_dead(self, job: dict[str, Any]) -> bool:
        elapsed = time.monotonic() - self._started_at
        if elapsed < _HEARTBEAT_GRACE_SEC:
            return False
        hb = job.get("last_heartbeat_at")
        if not hb:
            return True
        try:
            hb_time = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - hb_time).total_seconds() > _HEARTBEAT_TIMEOUT_SEC
        except Exception:
            return False

    def _handle_failure(self, job: dict[str, Any], error: str) -> None:
        log.warning("Job %s: %s", self._job_id, error)
        self._client.cancel_job(self._provider_job_id)
        self._on_failure(job, error, self._group_id)

    @staticmethod
    def _output_count(job: dict[str, Any]) -> int:
        import json
        raw = job.get("output_files") or "[]"
        try:
            parsed = json.loads(raw)
            return len(parsed) if isinstance(parsed, list) else 0
        except (json.JSONDecodeError, TypeError):
            return 0
