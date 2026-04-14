"""FailureHandler — retry on same endpoint -> failover to another machine -> give up.

Uses composition: receives repositories + registry at construction, no globals.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Callable
from uuid import uuid4

from serverV2.core.enums import SERVERLESS_TYPE_VALUES
from serverV2.core.models import DispatchContext, Machine, PlannedTask, RenderJob
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository

log = logging.getLogger(__name__)


class FailoverPicker:
    """Selects the best failover machine with cross-provider preference."""

    def pick(
        self,
        candidates: list[Machine],
        failed_machine_id: str,
        failed_machine_type: str,
    ) -> Machine | None:
        pool = [c for c in candidates if c.id != failed_machine_id]
        if not pool:
            return None

        serverless = [m for m in pool if m.machine_type in SERVERLESS_TYPE_VALUES]
        non_serverless = [m for m in pool if m.machine_type not in SERVERLESS_TYPE_VALUES]

        if failed_machine_type == "modal_serverless":
            vast = [m for m in serverless if m.machine_type == "vast_serverless"]
            if vast:
                return vast[0]
            other = [m for m in serverless if m.machine_type != "modal_serverless"]
            return other[0] if other else (non_serverless[0] if non_serverless else None)

        if failed_machine_type == "vast_serverless":
            other_vast = [m for m in serverless if m.machine_type == "vast_serverless"]
            if other_vast:
                return other_vast[0]
            return non_serverless[0] if non_serverless else None

        if serverless:
            vast = [m for m in serverless if m.machine_type == "vast_serverless"]
            return vast[0] if vast else serverless[0]
        return pool[0] if pool else None


class FailureHandler:

    def __init__(
        self,
        job_repo: JobRepository,
        machine_repo: MachineRepository,
        dispatch_fn: Callable[[PlannedTask, DispatchContext], Any],
        blend_url_fn: Callable[[str, str, str], str],
    ) -> None:
        self._job_repo = job_repo
        self._machine_repo = machine_repo
        self._dispatch_fn = dispatch_fn
        self._blend_url_fn = blend_url_fn
        self._failover_picker = FailoverPicker()

    def handle(self, job: dict[str, Any] | RenderJob, error: str, group_id: str) -> str | None:
        rj = job if isinstance(job, RenderJob) else RenderJob.from_row(job)
        raw = job if isinstance(job, dict) else self._job_repo.get_raw_by_id(rj.job_id)
        if raw is None:
            return None

        fresh = self._job_repo.get_raw_by_id(rj.job_id)
        if fresh and fresh.get("status") in ("cancelled", "done"):
            log.info("Job %s already %s — skipping failover", rj.job_id, fresh["status"])
            return None

        if group_id:
            from serverV2.infrastructure.db import query_one
            grp = query_one("SELECT status FROM render_groups WHERE id = %s", (group_id,))
            if grp and grp.get("status") == "cancelled":
                log.info("Group %s cancelled — skipping failover for job %s", group_id, rj.job_id)
                self._job_repo.mark_failed(rj.job_id, "Group cancelled")
                return None

        remaining = rj.remaining_frames()
        if remaining is None:
            self._job_repo.mark_done(rj.job_id)
            return None

        remaining_start, remaining_end = remaining

        if rj.can_retry_same_endpoint():
            retry_id = self._retry_same_endpoint(rj, remaining_start, remaining_end, error, group_id)
            if retry_id:
                return retry_id
            error = f"Same-endpoint retry failed ({error})"

        candidates = self._machine_repo.get_failover_candidates(rj.machine_id)
        candidate = self._failover_picker.pick(candidates, rj.machine_id, rj.machine_type)
        if candidate:
            return self._dispatch_failover(
                rj, raw, candidate, remaining_start, remaining_end, error, group_id,
            )

        self._job_repo.mark_failed(rj.job_id, error)
        log.error("Job %s: no machines available for failover", rj.job_id)
        return None

    def _retry_same_endpoint(
        self, rj: RenderJob, start: int, end: int, error: str, group_id: str,
    ) -> str | None:
        step = rj.frame_step or 1
        next_attempt = (rj.attempt or 0) + 1

        self._job_repo.set_retry(rj.job_id, next_attempt, rj.max_retries, error, start)
        log.warning("Job %s: retry %d/%d", rj.job_id, next_attempt, rj.max_retries)

        overrides_b64 = base64.b64encode((rj.render_overrides_json or "{}").encode()).decode()
        blend_url = self._blend_url_fn(rj.machine_type, group_id, rj.input_filename)

        try:
            self._dispatch_fn(
                PlannedTask(
                    machine_id=rj.machine_id, machine_type=rj.machine_type,
                    gpu_model="", gpu_vram_gb=0,
                    frame_start=start, frame_end=end, frame_step=step,
                    total_frames=((end - start) // step) + 1, power_score=0,
                ),
                DispatchContext(
                    group_id=group_id, input_filename=rj.input_filename,
                    render_overrides_b64=overrides_b64, blend_url=blend_url,
                ),
            )
            return rj.job_id
        except Exception as exc:
            log.error("Same-endpoint retry dispatch failed for %s: %s", rj.job_id, exc)
            return None

    def _dispatch_failover(
        self, rj: RenderJob, raw: dict[str, Any], machine: Machine,
        start: int, end: int, error: str, group_id: str,
    ) -> str:
        step = rj.frame_step or 1
        new_total = ((end - start) // step) + 1
        new_job_id = str(uuid4())

        self._job_repo.mark_failed(rj.job_id, f"Failed, migrating remaining frames ({error})")

        self._job_repo.create_failover_job(
            new_job_id=new_job_id,
            failover_machine_id=machine.id,
            group_id=group_id,
            input_filename=raw.get("input_filename", ""),
            total_frames=new_total,
            frame_start=start,
            frame_end=end,
            frame_step=step,
            render_overrides_json=raw.get("render_overrides_json") or "{}",
            max_retries=raw.get("max_retries") or 0,
            priority=raw.get("priority") or 0,
            chunk_index=raw.get("chunk_index"),
        )

        log.info("Job %s -> failover %s on %s", rj.job_id, new_job_id, machine.gpu_model)

        overrides_b64 = base64.b64encode((rj.render_overrides_json or "{}").encode()).decode()
        blend_url = self._blend_url_fn(machine.machine_type, group_id, raw.get("input_filename", ""))

        try:
            self._dispatch_fn(
                PlannedTask(
                    machine_id=machine.id, machine_type=machine.machine_type,
                    gpu_model=machine.gpu_model, gpu_vram_gb=machine.gpu_vram_gb,
                    frame_start=start, frame_end=end, frame_step=step,
                    total_frames=new_total, power_score=0,
                ),
                DispatchContext(
                    group_id=group_id, input_filename=raw.get("input_filename", ""),
                    render_overrides_b64=overrides_b64, blend_url=blend_url,
                ),
            )
        except Exception as exc:
            log.error("Failover dispatch failed for %s: %s", new_job_id, exc)
            self._job_repo.mark_failed(new_job_id, f"Failover dispatch failed: {exc}")

        return new_job_id
