"""
JobMonitor — background thread for stuck job detection.

Modal workers call back directly (same HTTP callbacks as RunPod workers),
so there is no need to actively poll Modal for status. This monitor only
checks the DB for stuck/stale jobs and delegates failure handling to
DispatchCoordinator.
"""

from __future__ import annotations

import logging
import threading
import time

from infrastructure.db import query_one
from services.modal.config import ModalConfig

log = logging.getLogger(__name__)

_JOB_QUERY = """
    SELECT status, attempt, max_retries, frame_start, frame_end,
           frame_step, rendered_frames, input_filename,
           render_overrides_json, chunk_index, chunk_size_frames, priority
    FROM jobs WHERE id = %s
"""


class JobMonitor:
    def __init__(
        self,
        job_id: str,
        provider_job_id: str,
        machine_id: str,
        blend_url: str,
        render_overrides_b64: str,
        group_id: str,
        config: ModalConfig,
    ) -> None:
        self._job_id = job_id
        self._provider_job_id = provider_job_id
        self._machine_id = machine_id
        self._blend_url = blend_url
        self._render_overrides_b64 = render_overrides_b64
        self._group_id = group_id
        self._cfg = config

    def start(self) -> None:
        t = threading.Thread(
            target=self._run, daemon=True,
            name=f"modal-mon-{self._job_id[:8]}",
        )
        t.start()

    def _run(self) -> None:
        started_at = time.monotonic()
        last_rendered_frames: int | None = None
        last_frame_change_at = time.monotonic()

        while True:
            time.sleep(self._cfg.monitor_interval_sec)
            try:
                job = query_one(_JOB_QUERY, (self._job_id,))
                if not job:
                    log.warning(f"Monitor: job {self._job_id} not found in DB, stopping")
                    break

                local_status = job["status"]

                if local_status in ("done", "failed", "cancelled"):
                    log.info(f"Monitor: job {self._job_id} is {local_status}, stopping")
                    break

                elapsed = time.monotonic() - started_at

                if local_status == "pending" and elapsed > self._cfg.in_queue_timeout_sec:
                    self._handle_failure(
                        job,
                        f"Modal job stuck in pending for {elapsed:.0f}s — routing to failover",
                    )
                    break

                if local_status == "running":
                    cur_frames = job.get("rendered_frames") or 0
                    if cur_frames != last_rendered_frames:
                        last_rendered_frames = cur_frames
                        last_frame_change_at = time.monotonic()
                    elif time.monotonic() - last_frame_change_at > self._cfg.in_progress_stale_sec:
                        self._handle_failure(
                            job,
                            f"Modal job running but no new frames for "
                            f"{self._cfg.in_progress_stale_sec / 60:.0f} min — cancelling",
                        )
                        break

            except Exception as e:
                log.error(f"Modal monitor error for job {self._job_id}: {e}")

    def _handle_failure(self, job: dict, error: str) -> None:
        from scheduling.dispatch_coordinator import coordinator

        log.warning(f"Job {self._job_id}: {error}")
        coordinator.handle_failure(
            job_id=self._job_id,
            job=job,
            error=error,
            blend_url=self._blend_url,
            render_overrides_b64=self._render_overrides_b64,
            failed_machine_id=self._machine_id,
            group_id=self._group_id,
        )
