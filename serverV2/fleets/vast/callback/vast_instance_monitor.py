"""VastInstanceMonitor — the per-job tick narrative for Vast instances.

One instance + one thread per active Vast job.  Owns the ordered list of
decisions (the "blocks") and delegates count math, liveness, snapshot
writing, and Vast-API state classification to the injected helpers.
Zero retry logic — failure/success events are routed out via the
injected callables.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

import psycopg2.pool

from serverV2.config import VastConfig
from serverV2.fleets.shared.job_counts import JobCounts
from serverV2.fleets.shared.liveness_check import LivenessCheck
from serverV2.fleets.shared.pre_render_stall_detector import (
    HeartbeatWindow,
    IPreRenderStallDetector,
    StallReason,
)
from serverV2.fleets.vast.callback.vast_snapshot_writer import VastSnapshotWriter
from serverV2.fleets.vast.callback.vast_status_classifier import VastStatusClassifier
from serverV2.fleets.vast.client import VastClient
from serverV2.repositories.heartbeat_repository import HeartbeatRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)

_MAX_CONSECUTIVE_API_ERRORS = 10
_EXIT_CALLBACK_WAIT_SEC = 60
_EXIT_POLL_SEC = 5


class VastInstanceMonitor:

    def __init__(
        self,
        *,
        job_id: str,
        instance_id: int,
        group_id: str,
        config: VastConfig,
        client: VastClient,
        orchestrator: "RenderOrchestrator",
        counts: JobCounts,
        liveness: LivenessCheck,
        snapshot: VastSnapshotWriter,
        status: VastStatusClassifier,
        stall_detector: IPreRenderStallDetector,
        heartbeat_repo: HeartbeatRepository,
        on_failure: Callable[[str, str], None],
        on_success: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        self._job_id = job_id
        self._instance_id = instance_id
        self._group_id = group_id
        self._cfg = config
        self._client = client
        self._orchestrator = orchestrator
        self._counts = counts
        self._liveness = liveness
        self._snapshot = snapshot
        self._status = status
        self._stall_detector = stall_detector
        self._heartbeat_repo = heartbeat_repo
        self._on_failure = on_failure
        self._on_success = on_success
        self._stop = stop_event

        self._started_at = time.monotonic()
        self._became_running_at: float | None = None
        self._consecutive_api_errors = 0

    # ------------------------------------------------------------------
    # Thread driver
    # ------------------------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._cfg.poll_interval_sec)
            if self._stop.is_set():
                log.info("Job %s: vast monitor stopped by cancel", self._job_id)
                self._snapshot.remove()
                break
            try:
                if self._tick():
                    break
                self._consecutive_api_errors = 0
            except psycopg2.pool.PoolError as e:
                # Transient server-internal pressure, not a Vast API issue.
                # Don't increment the API-error counter — we'd kill healthy
                # jobs because the local DB pool was briefly full.
                log.warning(
                    "Vast poll for job %s skipped — DB pool exhausted: %s",
                    self._job_id, e,
                )
            except Exception as e:
                self._consecutive_api_errors += 1
                log.error(
                    "Vast poll error for job %s (%d/%d): %s",
                    self._job_id, self._consecutive_api_errors,
                    _MAX_CONSECUTIVE_API_ERRORS, e,
                )
                if self._consecutive_api_errors >= _MAX_CONSECUTIVE_API_ERRORS:
                    self._on_failure(
                        self._job_id,
                        f"Vast API unreachable for {self._consecutive_api_errors} consecutive polls",
                    )
                    self._snapshot.remove()
                    break

    # ------------------------------------------------------------------
    # Ordered decision blocks — first terminal action ends the tick
    # ------------------------------------------------------------------

    def _tick(self) -> bool:
        inst = self._client.instances.get(self._instance_id)
        elapsed = time.monotonic() - self._started_at

        # Block: instance disappeared from Vast entirely.
        if inst is None:
            return self._on_instance_gone()

        actual_status = self._status.normalize(str(inst.get("actual_status") or ""))
        status_msg = str(inst.get("status_msg") or "")

        # Block: fatal daemon-level error message.
        if self._status.has_fatal_error(actual_status, status_msg):
            return self._on_fatal(inst)

        job = self._orchestrator.get_job_raw(self._job_id)
        if not job:
            self._client.instances.destroy(self._instance_id)
            self._snapshot.remove()
            return True

        local_status = job["status"]
        self._liveness.note_activity(self._counts.rendered(self._job_id, job))
        self._snapshot.write(job, actual_status, elapsed)

        # Block: DB says this job is already over.
        if local_status in ("done", "failed", "cancelled"):
            self._client.instances.destroy(self._instance_id)
            if local_status == "failed":
                error = str(job.get("error") or "Worker reported failure")
                self._on_failure(self._job_id, error)
            self._snapshot.remove()
            return True

        # Block: group went terminal while this job was still active.
        if self._group_id:
            group_status = self._orchestrator.get_group_status(self._group_id)
            if group_status in ("done", "failed", "cancelled"):
                log.info(
                    "Group %s is %s — cleaning up Vast job %s",
                    self._group_id, group_status, self._job_id,
                )
                self._client.instances.destroy(self._instance_id)
                # Orchestrator handles marking the job to match the group
                # state (handle_chunk_failed sees group terminal → skip
                # retry → mark_failed).
                self._on_failure(
                    self._job_id, f"Group was {group_status}",
                )
                self._snapshot.remove()
                return True

        # Block: success by verified uploads.
        if self._counts.is_complete(job) and local_status == "running":
            self._on_success(self._job_id)
            self._client.instances.destroy(self._instance_id)
            self._snapshot.remove()
            return True

        # Block: startup timeout — container never reached running.
        if (self._became_running_at is None
                and not self._status.is_running(actual_status)
                and elapsed > self._cfg.startup_timeout_sec):
            return self._on_startup_timeout(actual_status, elapsed)

        # Block: container is running.
        if self._status.is_running(actual_status):
            if self._became_running_at is None:
                self._became_running_at = time.monotonic()
                self._liveness.mark_active()
                if job["status"] == "pending":
                    self._orchestrator.on_job_running(self._job_id)
            if self._liveness.is_stale():
                return self._on_stale()
            if self._liveness.heartbeat_dead(self._job_id):
                return self._on_heartbeat_dead()
            stall = self._evaluate_stall()
            if stall is not None:
                return self._on_stall(stall)

        # Block: container exited / stopped / offline.
        if self._status.is_exited(actual_status):
            return self._on_exited(actual_status)

        return False

    # ------------------------------------------------------------------
    # Terminal handlers
    # ------------------------------------------------------------------

    def _on_instance_gone(self) -> bool:
        self._snapshot.remove()
        job = self._orchestrator.get_job_raw(self._job_id)
        if not job or job["status"] in ("done", "failed", "cancelled"):
            return True
        if self._counts.is_complete(job):
            self._on_success(self._job_id)
            return True
        self._on_failure(self._job_id, "Vast.ai instance disappeared unexpectedly")
        return True

    def _on_fatal(self, inst: dict[str, Any]) -> bool:
        err = (
            f"Vast.ai fatal startup error "
            f"(status={inst.get('actual_status')}): "
            f"{str(inst.get('status_msg', ''))[:200]}"
        )
        self._client.instances.destroy(self._instance_id)
        self._snapshot.remove()
        self._on_failure(self._job_id, err)
        return True

    def _on_startup_timeout(self, actual_status: str, elapsed: float) -> bool:
        err = f"Vast.ai instance stuck in '{actual_status}' for {elapsed:.0f}s"
        self._client.instances.destroy(self._instance_id)
        self._snapshot.remove()
        self._on_failure(self._job_id, err)
        return True

    def _on_stale(self) -> bool:
        err = (
            f"Vast.ai job running but no new frames for "
            f"{self._cfg.in_progress_stale_sec / 60:.0f} min"
        )
        self._client.instances.destroy(self._instance_id)
        self._snapshot.remove()
        self._on_failure(self._job_id, err)
        return True

    def _on_heartbeat_dead(self) -> bool:
        err = "Worker heartbeat stopped while Vast instance shows running"
        self._client.instances.destroy(self._instance_id)
        self._snapshot.remove()
        self._on_failure(self._job_id, err)
        return True

    def _evaluate_stall(self) -> StallReason | None:
        samples = self._heartbeat_repo.get_recent(self._job_id, n=30)
        if not samples:
            return None
        window = HeartbeatWindow.from_raw(samples)
        elapsed = time.monotonic() - self._started_at
        return self._stall_detector.evaluate(window, elapsed)

    def _on_stall(self, reason: StallReason) -> bool:
        err = f"Pre-render stall ({reason.rule}): {reason.message}"
        log.warning("Job %s stalled: %s", self._job_id, err)
        self._client.instances.destroy(self._instance_id)
        self._snapshot.remove()
        self._on_failure(self._job_id, err)
        return True

    def _on_exited(self, actual_status: str) -> bool:
        self._client.instances.destroy(self._instance_id)
        job = self._orchestrator.get_job_raw(self._job_id)
        if not job:
            self._snapshot.remove()
            return True
        if self._counts.is_complete(job):
            self._on_success(self._job_id)
            self._snapshot.remove()
            return True
        final_status = self._wait_for_callback()
        if final_status not in ("done", "failed", "cancelled"):
            err = f"Vast.ai instance exited ({actual_status}) — callback did not arrive"
            self._on_failure(self._job_id, err)
        self._snapshot.remove()
        return True

    # ------------------------------------------------------------------
    # Short-lived post-exit wait for the worker's final callback
    # ------------------------------------------------------------------

    def _wait_for_callback(self) -> str:
        iterations = max(1, _EXIT_CALLBACK_WAIT_SEC // _EXIT_POLL_SEC)
        for _ in range(iterations):
            self._stop.wait(_EXIT_POLL_SEC)
            if self._stop.is_set():
                return "cancelled"
            status = self._orchestrator.get_job_status(self._job_id) or "unknown"
            if status in ("done", "failed", "cancelled"):
                return status
        return "unknown"
