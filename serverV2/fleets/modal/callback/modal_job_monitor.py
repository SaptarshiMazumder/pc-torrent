"""ModalJobMonitor — the per-job tick narrative.

One instance + one thread per active Modal job.  Owns the ordered list of
decisions (the "blocks") and delegates all math, liveness, and snapshot
writing to the injected helpers.  Zero retry logic — failure/success
events are routed out via the injected callables.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from serverV2.config import ModalConfig
from serverV2.fleets.modal.client import ModalClient
from serverV2.fleets.modal.callback.modal_snapshot_writer import ModalSnapshotWriter
from serverV2.fleets.shared.job_counts import JobCounts
from serverV2.fleets.shared.liveness_check import LivenessCheck
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


class ModalJobMonitor:

    def __init__(
        self,
        *,
        job_id: str,
        provider_job_id: str,
        group_id: str,
        config: ModalConfig,
        client: ModalClient,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        counts: JobCounts,
        liveness: LivenessCheck,
        snapshot: ModalSnapshotWriter,
        on_failure: Callable[[str, str], None],
        on_success: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        self._job_id = job_id
        self._provider_job_id = provider_job_id
        self._group_id = group_id
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._counts = counts
        self._liveness = liveness
        self._snapshot = snapshot
        self._on_failure = on_failure
        self._on_success = on_success
        self._stop = stop_event

        self._started_at = time.monotonic()

    # ------------------------------------------------------------------
    # Thread driver
    # ------------------------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._cfg.monitor_interval_sec)
            if self._stop.is_set():
                log.info("Job %s: monitor stopped by cancel", self._job_id)
                self._snapshot.remove()
                break
            try:
                if self._tick():
                    break
            except Exception as exc:
                log.error("Modal poll error for job %s: %s", self._job_id, exc)

    # ------------------------------------------------------------------
    # Ordered decision blocks — first terminal action ends the tick
    # ------------------------------------------------------------------

    def _tick(self) -> bool:
        job = self._job_repo.get_raw_by_id(self._job_id)
        elapsed = time.monotonic() - self._started_at

        if not job:
            self._snapshot.remove()
            return True

        local_status = str(job.get("status") or "")
        self._liveness.note_activity(self._counts.rendered(self._job_id, job))
        self._snapshot.write(job, elapsed)

        # Block: DB says this job is already over.
        if local_status in ("done", "failed", "cancelled"):
            if local_status == "cancelled":
                self._client.cancel_job(self._provider_job_id)
            elif local_status == "failed":
                error = str(job.get("error") or "Worker reported failure")
                self._on_failure(self._job_id, error)
            self._snapshot.remove()
            return True

        # Block: Group went terminal while this job was still active.
        if self._group_id:
            group = self._group_repo.get_by_id(self._group_id)
            if group and group.get("status") in ("done", "failed", "cancelled"):
                group_status = group["status"]
                log.info(
                    "Group %s is %s — cleaning up Modal job %s",
                    self._group_id, group_status, self._job_id,
                )
                self._client.cancel_job(self._provider_job_id)
                self._job_repo.update_status(
                    self._job_id, group_status,
                    error=f"Group was {group_status}",
                )
                self._snapshot.remove()
                return True

        # Block: success by verified uploads.
        if self._counts.is_complete(job):
            self._handle_success()
            return True

        # Block: dispatch-lost — pending past queue window AND worker silent.
        if local_status == "pending" and elapsed > self._cfg.in_queue_timeout_sec:
            if self._counts.is_complete(job):
                self._handle_success()
                return True
            if self._liveness.heartbeat_dead(self._job_id):
                self._handle_failure(
                    f"Modal dispatch lost — pending for {elapsed:.0f}s with no heartbeat"
                )
                return True

        # Block: heartbeat dead while running.
        if local_status == "running" and self._liveness.heartbeat_dead(self._job_id):
            if self._counts.is_complete(job):
                self._handle_success()
                return True
            self._handle_failure("Modal job heartbeat dead")
            return True

        # Block: frame-progress staleness (only fires after first frame uploads).
        if local_status == "running" and self._liveness.is_stale():
            if self._counts.is_complete(job):
                self._handle_success()
                return True
            self._handle_failure(
                f"Modal job stale — no new frames for {self._cfg.in_progress_stale_sec / 60:.0f} min"
            )
            return True

        return False

    # ------------------------------------------------------------------
    # Terminal plumbing
    # ------------------------------------------------------------------

    def _handle_failure(self, error: str) -> None:
        log.warning("Job %s: %s", self._job_id, error)
        self._client.cancel_job(self._provider_job_id)
        self._snapshot.remove()
        self._on_failure(self._job_id, error)

    def _handle_success(self) -> None:
        """Terminate the Modal function and notify the callback router.
        Cancelling the Modal job kills the container so the worker doesn't
        keep retrying status updates after we've already accepted the files.
        """
        self._client.cancel_job(self._provider_job_id)
        self._snapshot.remove()
        self._on_success(self._job_id)
