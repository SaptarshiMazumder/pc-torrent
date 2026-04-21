"""VastCallbackHandler — background poller for Vast instances.

Monitors a rented instance, detects completion / failure / staleness,
and reports via _on_failure(job_id, error).  Zero retry logic — that
belongs to the orchestrator.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from serverV2.config import VastConfig
from serverV2.core.models import InstanceSnapshot
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.vast.client import VastClient
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)

_FATAL_MSG_FRAGMENTS = (
    "error response from daemon",
    "oci runtime",
    "failed to create",
    "unresolvable cdi devices",
    "failed to inject",
    "no compatible cycles gpu",
    "cuda error",
    "failed to start container",
)
_EXIT_CALLBACK_WAIT_SEC = 60
_EXIT_POLL_SEC = 5
_MAX_CONSECUTIVE_API_ERRORS = 10


class VastCallbackHandler:

    def __init__(
        self,
        config: VastConfig,
        client: VastClient,
        job_repo: JobRepository,
        on_failure: Callable[[str, str], None],
        registry: InstanceRegistry | None = None,
    ) -> None:
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._on_failure = on_failure
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

        instance_id = int(provider_job_id)
        poller = _InstancePoller(
            job_id=job_id,
            instance_id=instance_id,
            config=self._cfg,
            client=self._client,
            job_repo=self._job_repo,
            on_failure=self._on_failure,
            registry=self._registry,
            stop_event=stop_event,
        )

        def _run_and_cleanup() -> None:
            try:
                poller.run()
            finally:
                poller._remove_snapshot()
                with self._monitors_lock:
                    self._monitors.pop(job_id, None)

        t = threading.Thread(
            target=_run_and_cleanup, daemon=True,
            name=f"vast-poll-{job_id[:8]}",
        )
        t.start()

    def stop_monitoring(self, job_id: str) -> None:
        """Signal a monitor to stop immediately."""
        with self._monitors_lock:
            event = self._monitors.get(job_id)
        if event:
            event.set()
            log.info("Signalled vast monitor for job %s to stop", job_id)

    def stop_all(self) -> None:
        """Signal all active monitors to stop."""
        with self._monitors_lock:
            for event in self._monitors.values():
                event.set()
            count = len(self._monitors)
        if count:
            log.info("Signalled %d vast monitors to stop", count)


class _InstancePoller:

    def __init__(
        self,
        job_id: str,
        instance_id: int,
        config: VastConfig,
        client: VastClient,
        job_repo: JobRepository,
        on_failure: Callable[[str, str], None],
        registry: InstanceRegistry | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._job_id = job_id
        self._instance_id = instance_id
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._on_failure = on_failure
        self._registry = registry
        self._stop = stop_event or threading.Event()

        self._started_at = time.monotonic()
        self._became_running_at: float | None = None
        self._last_rendered_frames: int | None = None
        self._last_frame_change_at = time.monotonic()
        self._consecutive_api_errors = 0

    def run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._cfg.poll_interval_sec)
            if self._stop.is_set():
                log.info("Job %s: vast monitor stopped by cancel", self._job_id)
                self._remove_snapshot()
                break
            try:
                if self._tick():
                    break
                self._consecutive_api_errors = 0
            except Exception as e:
                self._consecutive_api_errors += 1
                log.error("Vast poll error for job %s (%d/%d): %s",
                          self._job_id, self._consecutive_api_errors,
                          _MAX_CONSECUTIVE_API_ERRORS, e)
                if self._consecutive_api_errors >= _MAX_CONSECUTIVE_API_ERRORS:
                    self._on_failure(
                        self._job_id,
                        f"Vast API unreachable for {self._consecutive_api_errors} consecutive polls",
                    )
                    self._remove_snapshot()
                    break

    def _tick(self) -> bool:
        inst = self._client.instances.get(self._instance_id)
        elapsed = time.monotonic() - self._started_at

        if inst is None:
            return self._on_instance_gone()

        actual_status = str(inst.get("actual_status") or "").lower()
        status_msg = str(inst.get("status_msg") or "").lower()

        if self._has_fatal_error(actual_status, status_msg):
            return self._on_fatal(inst)

        job = self._job_repo.get_raw_by_id(self._job_id)
        if not job:
            self._client.instances.destroy(self._instance_id)
            self._remove_snapshot()
            return True

        local_status = job["status"]
        self._write_snapshot(job, actual_status, elapsed)

        if local_status in ("done", "failed", "cancelled"):
            self._client.instances.destroy(self._instance_id)
            if local_status == "failed":
                error = str(job.get("error") or "Worker reported failure")
                self._on_failure(self._job_id, error)
            self._remove_snapshot()
            return True

        if self._all_frames_done(job) and local_status == "running":
            self._job_repo.mark_done(self._job_id)
            self._client.instances.destroy(self._instance_id)
            self._remove_snapshot()
            return True

        if (self._became_running_at is None
                and actual_status != "running"
                and elapsed > self._cfg.startup_timeout_sec):
            return self._on_startup_timeout(actual_status, elapsed)

        if actual_status == "running":
            if self._became_running_at is None:
                self._became_running_at = time.monotonic()
                if job["status"] == "pending":
                    self._job_repo.update_status(self._job_id, "running")
            if self._is_stale(job):
                return self._on_stale()
            if self._is_heartbeat_dead(job):
                return self._on_heartbeat_dead()

        if actual_status in ("exited", "stopped", "offline"):
            return self._on_exited(actual_status)

        return False

    # ---- state handlers ----

    def _on_instance_gone(self) -> bool:
        self._remove_snapshot()
        job = self._job_repo.get_raw_by_id(self._job_id)
        if not job or job["status"] in ("done", "failed", "cancelled"):
            return True
        if self._all_frames_done(job):
            self._job_repo.mark_done(self._job_id)
            return True
        self._on_failure(self._job_id, "Vast.ai instance disappeared unexpectedly")
        return True

    def _on_fatal(self, inst: dict[str, Any]) -> bool:
        err = f"Vast.ai fatal startup error (status={inst.get('actual_status')}): {str(inst.get('status_msg', ''))[:200]}"
        self._client.instances.destroy(self._instance_id)
        self._remove_snapshot()
        self._on_failure(self._job_id, err)
        return True

    def _on_startup_timeout(self, actual_status: str, elapsed: float) -> bool:
        err = f"Vast.ai instance stuck in '{actual_status}' for {elapsed:.0f}s"
        self._client.instances.destroy(self._instance_id)
        self._remove_snapshot()
        self._on_failure(self._job_id, err)
        return True

    def _on_stale(self) -> bool:
        err = f"Vast.ai job running but no new frames for {self._cfg.in_progress_stale_sec / 60:.0f} min"
        self._client.instances.destroy(self._instance_id)
        self._remove_snapshot()
        self._on_failure(self._job_id, err)
        return True

    def _on_heartbeat_dead(self) -> bool:
        err = "Worker heartbeat stopped while Vast instance shows running"
        self._client.instances.destroy(self._instance_id)
        self._remove_snapshot()
        self._on_failure(self._job_id, err)
        return True

    def _on_exited(self, actual_status: str) -> bool:
        self._client.instances.destroy(self._instance_id)
        job = self._job_repo.get_raw_by_id(self._job_id)
        if not job:
            self._remove_snapshot()
            return True
        if self._all_frames_done(job):
            self._job_repo.mark_done(self._job_id)
            self._remove_snapshot()
            return True
        final_status = self._wait_for_callback()
        if final_status not in ("done", "failed", "cancelled"):
            err = f"Vast.ai instance exited ({actual_status}) — callback did not arrive"
            self._on_failure(self._job_id, err)
        self._remove_snapshot()
        return True

    # ---- snapshot ----

    def _write_snapshot(self, job: dict[str, Any], provider_status: str, elapsed: float) -> None:
        if not self._registry:
            return
        self._registry.update(self._job_id, InstanceSnapshot(
            job_id=self._job_id,
            fleet_type="vast_serverless",
            provider_status=provider_status,
            gpu_label=str(job.get("gpu_model") or "Vast GPU"),
            rendered_frames=job.get("rendered_frames") or 0,
            total_frames=job.get("total_frames") or 0,
            elapsed_sec=round(elapsed),
            error=job.get("error"),
        ))

    def _remove_snapshot(self) -> None:
        if self._registry:
            self._registry.remove(self._job_id)

    # ---- helpers ----

    def _has_fatal_error(self, actual_status: str, status_msg: str) -> bool:
        return (
            actual_status not in ("running", "exited", "stopped", "offline")
            and any(f in status_msg for f in _FATAL_MSG_FRAGMENTS)
        )

    def _all_frames_done(self, job: dict[str, Any]) -> bool:
        total = job.get("total_frames") or 0
        rendered = job.get("rendered_frames") or 0
        return total > 0 and rendered >= total

    def _is_stale(self, job: dict[str, Any]) -> bool:
        if job["status"] != "running":
            return False
        cur = job.get("rendered_frames") or 0
        if cur != self._last_rendered_frames:
            self._last_rendered_frames = cur
            self._last_frame_change_at = time.monotonic()
            return False
        return time.monotonic() - self._last_frame_change_at > self._cfg.in_progress_stale_sec

    def _is_heartbeat_dead(self, job: dict[str, Any]) -> bool:
        from serverV2.infrastructure import heartbeat_store
        if self._became_running_at is None:
            return False
        if time.monotonic() - self._became_running_at < self._cfg.heartbeat_grace_sec:
            return False
        alive = heartbeat_store.is_alive(self._job_id)
        if alive is None:
            return False  # Redis unavailable — don't false-positive kill jobs
        return not alive

    def _wait_for_callback(self) -> str:
        iterations = max(1, _EXIT_CALLBACK_WAIT_SEC // _EXIT_POLL_SEC)
        for _ in range(iterations):
            self._stop.wait(_EXIT_POLL_SEC)
            if self._stop.is_set():
                return "cancelled"
            job = self._job_repo.get_raw_by_id(self._job_id)
            status = job["status"] if job else "unknown"
            if status in ("done", "failed", "cancelled"):
                return status
        return "unknown"
