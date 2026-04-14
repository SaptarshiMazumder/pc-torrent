"""VastCallbackHandler — background poller for Vast instances.

Monitors a rented instance, detects completion / failure / staleness,
and reports outcomes via the on_success / on_failure callables injected
at construction time (composition, not inheritance).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from serverV2.config import VastConfig
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


class VastCallbackHandler:

    def __init__(
        self,
        config: VastConfig,
        client: VastClient,
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
        instance_id = int(provider_job_id)
        poller = _InstancePoller(
            job_id=job_id,
            instance_id=instance_id,
            group_id=group_id,
            config=self._cfg,
            client=self._client,
            job_repo=self._job_repo,
            on_failure=self._on_failure,
        )
        t = threading.Thread(
            target=poller.run, daemon=True,
            name=f"vast-poll-{job_id[:8]}",
        )
        t.start()


class _InstancePoller:
    """Encapsulates the state machine for one Vast instance poll loop."""

    def __init__(
        self,
        job_id: str,
        instance_id: int,
        group_id: str,
        config: VastConfig,
        client: VastClient,
        job_repo: JobRepository,
        on_failure: Callable[[dict[str, Any], str, str], None],
    ) -> None:
        self._job_id = job_id
        self._instance_id = instance_id
        self._group_id = group_id
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._on_failure = on_failure

        self._started_at = time.monotonic()
        self._became_running_at: float | None = None
        self._last_rendered_frames: int | None = None
        self._last_frame_change_at = time.monotonic()

    def run(self) -> None:
        while True:
            time.sleep(self._cfg.poll_interval_sec)
            try:
                if self._tick():
                    break
            except Exception as e:
                log.error("Vast poll error for job %s: %s", self._job_id, e)

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
            return True

        local_status = job["status"]

        if local_status in ("done", "failed", "cancelled"):
            self._client.instances.destroy(self._instance_id)
            return True

        if self._all_frames_done(job) and local_status == "running":
            self._job_repo.mark_done(self._job_id)
            self._client.instances.destroy(self._instance_id)
            return True

        if (self._became_running_at is None
                and actual_status != "running"
                and elapsed > self._cfg.startup_timeout_sec):
            return self._on_startup_timeout(actual_status, elapsed, job)

        if actual_status == "running":
            if self._became_running_at is None:
                self._became_running_at = time.monotonic()
                if job["status"] == "pending":
                    self._job_repo.update_status(self._job_id, "running")
            if self._is_stale(job):
                return self._on_stale(job)
            if self._is_heartbeat_dead(job):
                return self._on_heartbeat_dead(job)

        if actual_status in ("exited", "stopped", "offline"):
            return self._on_exited(actual_status, job)

        return False

    # ---- state handlers ----

    def _on_instance_gone(self) -> bool:
        job = self._job_repo.get_raw_by_id(self._job_id)
        if not job or job["status"] in ("done", "failed", "cancelled"):
            return True
        if self._all_frames_done(job):
            self._job_repo.mark_done(self._job_id)
            return True
        self._on_failure(job, "Vast.ai instance disappeared unexpectedly", self._group_id)
        return True

    def _on_fatal(self, inst: dict[str, Any]) -> bool:
        err = f"Vast.ai fatal startup error (status={inst.get('actual_status')}): {str(inst.get('status_msg', ''))[:200]}"
        self._client.instances.destroy(self._instance_id)
        job = self._job_repo.get_raw_by_id(self._job_id)
        if job:
            self._on_failure(job, err, self._group_id)
        return True

    def _on_startup_timeout(self, actual_status: str, elapsed: float, job: dict[str, Any]) -> bool:
        err = f"Vast.ai instance stuck in '{actual_status}' for {elapsed:.0f}s"
        self._client.instances.destroy(self._instance_id)
        self._on_failure(job, err, self._group_id)
        return True

    def _on_stale(self, job: dict[str, Any]) -> bool:
        err = f"Vast.ai job running but no new frames for {self._cfg.in_progress_stale_sec / 60:.0f} min"
        self._client.instances.destroy(self._instance_id)
        self._on_failure(job, err, self._group_id)
        return True

    def _on_heartbeat_dead(self, job: dict[str, Any]) -> bool:
        err = "Worker heartbeat stopped while Vast instance shows running"
        self._client.instances.destroy(self._instance_id)
        self._on_failure(job, err, self._group_id)
        return True

    def _on_exited(self, actual_status: str, job: dict[str, Any]) -> bool:
        self._client.instances.destroy(self._instance_id)
        if self._all_frames_done(job):
            self._job_repo.mark_done(self._job_id)
            return True
        final_status = self._wait_for_callback()
        if final_status not in ("done", "failed", "cancelled"):
            err = f"Vast.ai instance exited ({actual_status}) — callback did not arrive"
            job_now = self._job_repo.get_raw_by_id(self._job_id)
            if job_now:
                self._on_failure(job_now, err, self._group_id)
        return True

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
        if self._became_running_at is None:
            return False
        if time.monotonic() - self._became_running_at < self._cfg.heartbeat_grace_sec:
            return False
        hb_at = job.get("last_heartbeat_at")
        if not hb_at:
            return True
        try:
            hb_time = datetime.fromisoformat(str(hb_at).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - hb_time).total_seconds() > self._cfg.heartbeat_timeout_sec
        except (ValueError, AttributeError):
            return True

    def _wait_for_callback(self) -> str:
        iterations = max(1, _EXIT_CALLBACK_WAIT_SEC // _EXIT_POLL_SEC)
        for _ in range(iterations):
            time.sleep(_EXIT_POLL_SEC)
            job = self._job_repo.get_raw_by_id(self._job_id)
            status = job["status"] if job else "unknown"
            if status in ("done", "failed", "cancelled"):
                return status
        return "unknown"
