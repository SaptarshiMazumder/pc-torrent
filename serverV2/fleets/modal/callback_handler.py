"""ModalCallbackHandler — background monitor for Modal jobs.

Detects completion / failure / staleness and reports via injected callables.
On failure: cancels the Modal job and notifies via _on_failure(job_id, error).
Zero retry logic — that belongs to the orchestrator.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from serverV2.config import ModalConfig
from serverV2.core.models import InstanceSnapshot
from serverV2.core.value_objects import parse_output_files
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.modal.client import ModalClient
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.progress_repository import ProgressRepository

log = logging.getLogger(__name__)

_HEARTBEAT_GRACE_SEC = 90
_HEARTBEAT_TIMEOUT_SEC = 45


class ModalCallbackHandler:

    def __init__(
        self,
        config: ModalConfig,
        client: ModalClient,
        job_repo: JobRepository,
        heartbeat_repo: HeartbeatRepository,
        progress_repo: ProgressRepository,
        on_failure: Callable[[str, str], None],
        on_success: Callable[[str], None],
        registry: InstanceRegistry | None = None,
    ) -> None:
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._heartbeats = heartbeat_repo
        self._progress = progress_repo
        self._on_failure = on_failure
        self._on_success = on_success
        self._registry = registry
        self._monitors: dict[str, threading.Event] = {}
        self._monitors_lock = threading.Lock()

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
        stop_event = threading.Event()
        with self._monitors_lock:
            self._monitors[job_id] = stop_event

        monitor = _JobMonitor(
            job_id=job_id,
            provider_job_id=provider_job_id,
            config=self._cfg,
            client=self._client,
            job_repo=self._job_repo,
            heartbeat_repo=self._heartbeats,
            progress_repo=self._progress,
            on_failure=self._on_failure,
            on_success=self._on_success,
            registry=self._registry,
            stop_event=stop_event,
        )

        def _run_and_cleanup() -> None:
            try:
                monitor.run()
            finally:
                monitor._remove_snapshot()
                with self._monitors_lock:
                    self._monitors.pop(job_id, None)

        t = threading.Thread(
            target=_run_and_cleanup, daemon=True,
            name=f"modal-mon-{job_id[:8]}",
        )
        t.start()

    def stop_monitoring(self, job_id: str) -> None:
        """Signal a monitor to stop immediately."""
        with self._monitors_lock:
            event = self._monitors.get(job_id)
        if event:
            event.set()
            log.info("Signalled monitor for job %s to stop", job_id)

    def stop_all(self) -> None:
        """Signal all active monitors to stop."""
        with self._monitors_lock:
            for event in self._monitors.values():
                event.set()
            count = len(self._monitors)
        if count:
            log.info("Signalled %d modal monitors to stop", count)


class _JobMonitor:

    def __init__(
        self,
        job_id: str,
        provider_job_id: str,
        config: ModalConfig,
        client: ModalClient,
        job_repo: JobRepository,
        heartbeat_repo: HeartbeatRepository,
        progress_repo: ProgressRepository,
        on_failure: Callable[[str, str], None],
        on_success: Callable[[str], None],
        registry: InstanceRegistry | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._job_id = job_id
        self._provider_job_id = provider_job_id
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._heartbeats = heartbeat_repo
        self._progress = progress_repo
        self._on_failure = on_failure
        self._on_success = on_success
        self._registry = registry
        self._stop = stop_event or threading.Event()

        self._started_at = time.monotonic()
        self._last_activity_at = time.monotonic()
        self._last_activity_marker: tuple[int, ...] | None = None

    def run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._cfg.monitor_interval_sec)
            if self._stop.is_set():
                log.info("Job %s: monitor stopped by cancel", self._job_id)
                self._remove_snapshot()
                break
            try:
                if self._tick():
                    break
            except Exception as exc:
                log.error("Modal poll error for job %s: %s", self._job_id, exc)

    def _tick(self) -> bool:
        job = self._job_repo.get_raw_by_id(self._job_id)
        elapsed = time.monotonic() - self._started_at

        if not job:
            self._remove_snapshot()
            return True

        local_status = str(job.get("status") or "")
        self._update_activity(job)
        self._write_snapshot(job, elapsed)

        if local_status in ("done", "failed", "cancelled"):
            if local_status == "cancelled":
                self._client.cancel_job(self._provider_job_id)
            elif local_status == "failed":
                error = str(job.get("error") or "Worker reported failure")
                self._on_failure(self._job_id, error)
            self._remove_snapshot()
            return True

        if self._is_complete(job):
            self._on_success(self._job_id)
            self._remove_snapshot()
            return True

        if local_status == "pending" and elapsed > self._cfg.in_queue_timeout_sec:
            if self._is_complete(job):
                self._on_success(self._job_id)
                return True
            self._handle_failure(f"Modal job stuck in pending for {elapsed:.0f}s")
            return True

        if local_status == "running" and self._is_heartbeat_dead(job):
            if self._is_complete(job):
                self._on_success(self._job_id)
                return True
            self._handle_failure(f"Modal job heartbeat dead (no ping for >{_HEARTBEAT_TIMEOUT_SEC}s)")
            return True

        if local_status == "running" and self._is_stale():
            if self._is_complete(job):
                self._on_success(self._job_id)
                return True
            self._handle_failure(f"Modal job stale — no new frames for {self._cfg.in_progress_stale_sec / 60:.0f} min")
            return True

        return False

    def _uploaded_count(self, job: dict[str, Any]) -> int:
        """Verified upload count — only files registered in output_files.
        Use for completion decisions; worker self-reports don't belong here."""
        return len(parse_output_files(job.get("output_files")))

    def _rendered_count(self, job: dict[str, Any]) -> int:
        """Best estimate for liveness/staleness.  Combines all signals.
        Do NOT use for completion decisions."""
        counts = [
            job.get("rendered_frames") or 0,
            self._uploaded_count(job),
        ]
        live = self._progress.get(self._job_id)
        if live is not None:
            counts.append(live.rendered_frames)
        return max(counts)

    def _update_activity(self, job: dict[str, Any]) -> None:
        marker = (self._rendered_count(job),)
        if marker != self._last_activity_marker:
            self._last_activity_marker = marker
            self._last_activity_at = time.monotonic()

    def _is_complete(self, job: dict[str, Any]) -> bool:
        total = job.get("total_frames") or 0
        if total <= 0:
            return False
        return self._uploaded_count(job) >= total

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._last_activity_at) > self._cfg.in_progress_stale_sec

    def _is_heartbeat_dead(self, job: dict[str, Any]) -> bool:
        elapsed = time.monotonic() - self._started_at
        if elapsed < _HEARTBEAT_GRACE_SEC:
            return False
        alive = self._heartbeats.is_alive(self._job_id)
        if alive is None:
            return False  # Redis unavailable — don't false-positive kill jobs
        return not alive

    def _write_snapshot(self, job: dict[str, Any], elapsed: float) -> None:
        if not self._registry:
            return
        self._registry.update(self._job_id, InstanceSnapshot(
            job_id=self._job_id,
            fleet_type="modal_serverless",
            provider_status=str(job.get("status") or "pending"),
            gpu_label=str(job.get("gpu_model") or "Modal GPU"),
            rendered_frames=job.get("rendered_frames") or 0,
            total_frames=job.get("total_frames") or 0,
            elapsed_sec=round(elapsed),
            error=job.get("error"),
        ))

    def _remove_snapshot(self) -> None:
        if self._registry:
            self._registry.remove(self._job_id)

    def _handle_failure(self, error: str) -> None:
        log.warning("Job %s: %s", self._job_id, error)
        self._client.cancel_job(self._provider_job_id)
        self._remove_snapshot()
        self._on_failure(self._job_id, error)
