"""
FailoverHandler — job failure + re-dispatch logic.

Encapsulates the failover sequence: mark failed -> find candidate -> insert
new job -> dispatch. Only fails over to other Vast machines (self-contained;
other providers have their own billing/lifecycle).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from domain.value_objects import now_iso
from infrastructure.db import execute, query_all
from services.vast.config import VastConfig

log = logging.getLogger(__name__)


class FailoverHandler:
    def __init__(self, config: VastConfig) -> None:
        self._cfg = config

    def handle(
        self,
        job_id: str,
        job: dict,
        error: str,
        blend_url: str,
        render_overrides_b64: str,
        failed_machine_id: str,
        group_id: str,
    ) -> str | None:
        """
        Mark the original job failed, then walk all available Vast machines in
        VRAM-ranked order until a dispatch succeeds. Each candidate gets one
        attempt; if it fails we move to the next.

        Returns the new job_id of the successful failover, or None if all fail.
        """
        from scheduling.dispatch_coordinator import coordinator

        rendered = max(0, job.get("rendered_frames") or 0)
        step = job.get("frame_step") or 1
        remaining_start = job["frame_start"] + rendered * step
        remaining_end = job["frame_end"]

        if not blend_url or remaining_start > remaining_end:
            execute(
                "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
                (now_iso(), error, job_id),
            )
            log.error(f"Vast job {job_id} failed with no frames left to migrate: {error}")
            return None

        execute(
            "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
            (now_iso(), f"Failed, migrating remaining frames ({error})", job_id),
        )

        candidates = query_all(
            """
            SELECT * FROM machines
            WHERE status = 'available'
              AND machine_type = 'vast_serverless'
              AND id != %s
            ORDER BY gpu_vram_gb DESC
            """,
            (failed_machine_id,),
        )
        if not candidates:
            log.error(f"Vast job {job_id}: no other Vast machines available for failover, frames lost")
            return None

        new_total = ((remaining_end - remaining_start) // step) + 1

        for candidate in candidates:
            new_job_id = str(uuid4())
            execute(
                """
                INSERT INTO jobs (
                    id, machine_id, group_id, input_filename, status,
                    total_frames, rendered_frames, output_files,
                    frame_start, frame_end, frame_step,
                    render_overrides_json, attempt, max_retries, priority,
                    chunk_index, chunk_size_frames, submitted_at
                )
                VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, 0, %s, %s, %s, %s, %s)
                """,
                (
                    new_job_id, candidate["id"], group_id, job.get("input_filename"),
                    new_total, remaining_start, remaining_end, step,
                    job.get("render_overrides_json") or "{}",
                    job.get("max_retries") or 0,
                    job.get("priority") or 0,
                    job.get("chunk_index"),
                    job.get("chunk_size_frames"),
                    now_iso(),
                ),
            )

            gpu_label = candidate.get("gpu_model", "?")
            machine_type = candidate.get("machine_type", "windows")
            log.info(
                f"Vast job {job_id} → failover attempt: new job {new_job_id} "
                f"on {gpu_label} ({machine_type})"
            )

            try:
                coordinator.dispatch(
                    job_id=new_job_id,
                    machine_id=candidate["id"],
                    machine_type=machine_type,
                    blend_url=blend_url,
                    frame_start=remaining_start,
                    frame_end=remaining_end,
                    frame_step=step,
                    render_overrides_b64=render_overrides_b64,
                    group_id=group_id,
                )
                log.info(f"Vast failover succeeded: job {new_job_id} on {gpu_label}")
                return new_job_id
            except Exception as exc:
                log.warning(
                    f"Vast failover dispatch failed for {new_job_id} on {gpu_label}: {exc} "
                    f"— trying next candidate"
                )
                execute(
                    "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
                    (now_iso(), f"Failover dispatch failed: {exc}", new_job_id),
                )

        log.error(f"Vast job {job_id}: all failover candidates exhausted, frames lost")
        return None
