"""
DispatchCoordinator — single source of truth for job dispatch, retry, and failover.

Uses the strategy pattern: each provider (RunPod, Modal, community desktop)
has a ProvisionStrategy that knows how to dispatch and cancel jobs.  The
coordinator looks up the right strategy by machine_type and delegates.

This class is the only place that knows how to:
  1. Route a job to the right provider via its strategy
  2. Retry on the same endpoint after failure
  3. Fail over to the best available machine from the pool
  4. Create the new job row in the DB for failover
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from domain.value_objects import SERVERLESS_TYPES, now_iso
from infrastructure.db import execute, query_all, query_one
from scheduling.strategies import get_strategy

log = logging.getLogger(__name__)


class DispatchCoordinator:
    """Coordinates job dispatch, retry, and failover across all providers."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch(
        self,
        job_id: str,
        machine_id: str,
        machine_type: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_b64: str,
        group_id: str = "",
    ) -> None:
        """Dispatch a job to the correct provider via its strategy."""
        strategy = get_strategy(machine_type)
        if not strategy.is_enabled():
            return
        strategy.dispatch(
            job_id=job_id,
            machine_id=machine_id,
            blend_url=blend_url,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            render_overrides_b64=render_overrides_b64,
            group_id=group_id,
        )

    def handle_failure(
        self,
        job_id: str,
        job: dict[str, Any],
        error: str,
        blend_url: str,
        render_overrides_b64: str,
        failed_machine_id: str,
        group_id: str,
    ) -> str | None:
        """
        Handle a failed job: retry on same endpoint first, then fail over.

        Returns the new job_id if a retry/failover was scheduled, else None.
        """
        rendered = max(0, job.get("rendered_frames") or 0)
        step = job.get("frame_step") or 1
        remaining_start = job["frame_start"] + rendered * step
        remaining_end = job["frame_end"]

        attempt = job.get("attempt") or 0
        max_retries = job.get("max_retries") or 0

        # ---- Attempt 1: retry on same endpoint ----
        if attempt < max_retries and blend_url and remaining_start <= remaining_end:
            next_attempt = attempt + 1
            execute(
                """
                UPDATE jobs
                SET status = 'pending', attempt = %s,
                    rendered_frames = 0, error = %s,
                    frame_start = %s
                WHERE id = %s
                """,
                (
                    next_attempt,
                    f"Retry {next_attempt}/{max_retries} (was: {error})",
                    remaining_start,
                    job_id,
                ),
            )
            log.warning(
                f"Job {job_id}: retry {next_attempt}/{max_retries} on same endpoint"
            )
            try:
                machine_type = self._machine_type_of(failed_machine_id)
                self.dispatch(
                    job_id=job_id,
                    machine_id=failed_machine_id,
                    machine_type=machine_type,
                    blend_url=blend_url,
                    frame_start=remaining_start,
                    frame_end=remaining_end,
                    frame_step=step,
                    render_overrides_b64=render_overrides_b64,
                    group_id=group_id,
                )
                return job_id
            except Exception as dispatch_err:
                log.error(
                    f"Same-endpoint retry dispatch failed for {job_id}: {dispatch_err}"
                )
                error = str(dispatch_err)

        # ---- Attempt 2: failover to another machine ----
        if not blend_url or remaining_start > remaining_end:
            execute(
                "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
                (now_iso(), error, job_id),
            )
            log.error(f"Job {job_id} failed, no failover possible: {error}")
            return None

        failed_machine_type = self._machine_type_of(failed_machine_id)
        return self._do_failover(
            job_id=job_id,
            job=job,
            error=error,
            remaining_start=remaining_start,
            remaining_end=remaining_end,
            step=step,
            blend_url=blend_url,
            render_overrides_b64=render_overrides_b64,
            failed_machine_id=failed_machine_id,
            failed_machine_type=failed_machine_type,
            group_id=group_id,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _do_failover(
        self,
        job_id: str,
        job: dict[str, Any],
        error: str,
        remaining_start: int,
        remaining_end: int,
        step: int,
        blend_url: str,
        render_overrides_b64: str,
        failed_machine_id: str,
        failed_machine_type: str | None = None,
        group_id: str = "",
    ) -> str | None:
        """Mark original job failed and create a new job on the best available machine."""
        execute(
            """
            UPDATE jobs
            SET status = 'failed', completed_at = %s, error = %s
            WHERE id = %s
            """,
            (now_iso(), f"Failed, migrating remaining frames ({error})", job_id),
        )

        failover_machine = self._find_failover_machine(failed_machine_id, failed_machine_type)
        if not failover_machine:
            log.error(f"Job {job_id}: no available machines for failover")
            return None

        new_job_id = str(uuid4())
        new_total = ((remaining_end - remaining_start) // step) + 1
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
                new_job_id,
                failover_machine["id"],
                group_id,
                job.get("input_filename"),
                new_total,
                remaining_start,
                remaining_end,
                step,
                job.get("render_overrides_json") or "{}",
                job.get("max_retries") or 0,
                job.get("priority") or 0,
                job.get("chunk_index"),
                job.get("chunk_size_frames"),
                now_iso(),
            ),
        )
        log.info(
            f"Job {job_id} -> failover new job {new_job_id} on "
            f"{failover_machine.get('gpu_model', '?')} "
            f"({failover_machine.get('machine_type', '?')})"
        )

        machine_type = failover_machine.get("machine_type", "windows")
        try:
            self.dispatch(
                job_id=new_job_id,
                machine_id=failover_machine["id"],
                machine_type=machine_type,
                blend_url=blend_url,
                frame_start=remaining_start,
                frame_end=remaining_end,
                frame_step=step,
                render_overrides_b64=render_overrides_b64,
                group_id=group_id,
            )
        except Exception as exc:
            log.error(f"Failover dispatch failed for {new_job_id}: {exc}")
            execute(
                "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
                (now_iso(), f"Failover dispatch failed: {exc}", new_job_id),
            )
        return new_job_id

    def _find_failover_machine(
        self,
        failed_machine_id: str,
        failed_machine_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the best available machine excluding the one that failed.

        When the failing machine is Modal, Vast is strongly preferred so that
        the workload moves to a different provider rather than another Modal
        container that may have the same underlying issue.
        """
        from scheduling.frame_distributor import filter_enabled_machines

        rows = query_all(
            """
            SELECT * FROM machines
            WHERE status = 'available' AND id != %s
            ORDER BY gpu_vram_gb DESC
            """,
            (failed_machine_id,),
        )
        rows = filter_enabled_machines(rows)
        if not rows:
            return None

        serverless = [r for r in rows if r.get("machine_type") in SERVERLESS_TYPES]

        if failed_machine_type == "modal_serverless":
            # Prefer Vast when Modal fails — move workload to a different provider.
            vast = [r for r in serverless if r.get("machine_type") == "vast_serverless"]
            if vast:
                log.info("Modal failover: routing to Vast (%s)", vast[0].get("id"))
                return vast[0]
            # No Vast available; try any non-Modal serverless machine.
            non_modal = [r for r in serverless if r.get("machine_type") != "modal_serverless"]
            if non_modal:
                log.info(
                    "Modal failover: no Vast available, routing to %s (%s)",
                    non_modal[0].get("machine_type"),
                    non_modal[0].get("id"),
                )
                return non_modal[0]
            # Last resort: community machines.
            non_serverless = [r for r in rows if r.get("machine_type") not in SERVERLESS_TYPES]
            if non_serverless:
                log.warning(
                    "Modal failover: no serverless alternative, routing to community machine %s",
                    non_serverless[0].get("id"),
                )
                return non_serverless[0]
            log.error("Modal failover: no machines available, job will be marked failed")
            return None

        if failed_machine_type == "vast_serverless":
            # Stay on Vast when a Vast job fails — pick a different Vast machine.
            other_vast = [r for r in serverless if r.get("machine_type") == "vast_serverless"]
            if other_vast:
                log.info("Vast failover: routing to another Vast machine (%s)", other_vast[0].get("id"))
                return other_vast[0]
            # No other Vast available; community machines as last resort.
            non_serverless = [r for r in rows if r.get("machine_type") not in SERVERLESS_TYPES]
            if non_serverless:
                log.warning(
                    "Vast failover: no other Vast machines available, routing to community machine %s",
                    non_serverless[0].get("id"),
                )
                return non_serverless[0]
            log.error("Vast failover: no machines available, job will be marked failed")
            return None

        return serverless[0] if serverless else rows[0]

    def _machine_type_of(self, machine_id: str) -> str:
        row = query_one("SELECT machine_type FROM machines WHERE id = %s", (machine_id,))
        return row["machine_type"] if row else "windows"


coordinator = DispatchCoordinator()
