"""
FleetManager — the HOW of job dispatch, persistence, retry, and failover.

The orchestrator decides WHAT to do; this module executes it.  All DB writes
for job lifecycle, provider-specific dispatch threading, blend URL construction,
machine availability queries, and failover machine selection live here.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from infrastructure.db import execute, query_all, query_one
from models.render_job import RenderJob
from models.value_objects import (
    MACHINE_STALE_SECONDS,
    SERVERLESS_TYPES,
    is_serverless,
    now_iso,
)
from scheduling.frame_distribution import filter_enabled_machines
from scheduling.strategies import get_strategy

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects shared with the orchestrator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedTask:
    machine_id: str
    machine_type: str
    gpu_model: str
    gpu_vram_gb: float
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    power_score: float
    chunk_index: int | None = None


@dataclass(frozen=True)
class DispatchResult:
    job_id: str
    machine_id: str
    status: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Status aggregation (was status_aggregator.py)
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = frozenset({"pending", "running"})
_TERMINAL_GROUP_STATUSES = frozenset({"cancelled", "failed"})


@dataclass(frozen=True)
class GroupStatusResult:
    status: str
    should_persist: bool


def compute_group_status(
    *,
    current_group_status: str,
    job_statuses: list[str],
    total_frames: int,
    total_rendered: int,
) -> GroupStatusResult:
    no_active = not any(s in _ACTIVE_STATUSES for s in job_statuses)
    all_frames_rendered = total_frames > 0 and total_rendered >= total_frames
    any_done = any(s == "done" for s in job_statuses)
    all_done = bool(job_statuses) and all(s == "done" for s in job_statuses)
    all_failed = bool(job_statuses) and all(s == "failed" for s in job_statuses)
    any_running = any(s == "running" for s in job_statuses)

    if current_group_status in _TERMINAL_GROUP_STATUSES:
        if no_active and any_done and all_frames_rendered:
            return GroupStatusResult(status="done", should_persist=current_group_status != "done")
        return GroupStatusResult(status=current_group_status, should_persist=False)

    if all_done or (no_active and any_done and all_frames_rendered):
        return GroupStatusResult(status="done", should_persist=current_group_status != "done")

    if any_running:
        return GroupStatusResult(status="running", should_persist=current_group_status == "pending")

    if no_active and all_failed:
        return GroupStatusResult(status="failed", should_persist=current_group_status != "failed")

    return GroupStatusResult(status=current_group_status, should_persist=False)


# ---------------------------------------------------------------------------
# FleetManager
# ---------------------------------------------------------------------------

class FleetManager:
    """Owns all side effects for job dispatch, persistence, and lifecycle."""

    # ------------------------------------------------------------------
    # Machine availability
    # ------------------------------------------------------------------

    def get_available_machines(self) -> list[dict[str, Any]]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=MACHINE_STALE_SECONDS)
        ).isoformat()
        execute(
            """
            UPDATE machines SET status = 'idle'
            WHERE status = 'available'
              AND machine_type NOT IN ('modal_serverless', 'vast_serverless')
              AND (last_seen_at IS NULL OR last_seen_at < %s)
            """,
            (cutoff,),
        )
        rows = query_all(
            """
            SELECT * FROM machines
            WHERE status = 'available'
              AND (machine_type IN ('modal_serverless', 'vast_serverless')
                   OR last_seen_at >= %s)
            ORDER BY gpu_vram_gb DESC
            """,
            (cutoff,),
        )
        return filter_enabled_machines(rows)

    # ------------------------------------------------------------------
    # Dispatch: persist jobs + send to providers
    # ------------------------------------------------------------------

    def dispatch_all(
        self,
        *,
        group_id: str,
        input_filename: str,
        tasks: list[PlannedTask],
        render_overrides_json: str,
        scheduling: dict[str, Any],
    ) -> list[DispatchResult]:
        max_retries = scheduling.get("max_retries_per_chunk", 0)
        priority = scheduling.get("priority", 0)
        overrides_b64 = base64.b64encode(render_overrides_json.encode()).decode()

        results: list[DispatchResult] = []
        tasks_by_type: dict[str, list[tuple[PlannedTask, str]]] = {}

        for task in tasks:
            job_id = str(uuid4())
            execute(
                """
                INSERT INTO jobs (
                    id, machine_id, group_id, input_filename, status,
                    total_frames, rendered_frames, output_files,
                    frame_start, frame_end, frame_step,
                    render_overrides_json, attempt, max_retries, priority,
                    chunk_index, submitted_at
                )
                VALUES (%s,%s,%s,%s,'pending',%s,0,'[]',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    job_id, task.machine_id, group_id, input_filename,
                    task.total_frames, task.frame_start, task.frame_end, task.frame_step,
                    render_overrides_json, 0, max_retries, priority,
                    task.chunk_index, now_iso(),
                ),
            )
            if not is_serverless(task.machine_type):
                execute(
                    "UPDATE machines SET status = 'processing' WHERE id = %s",
                    (task.machine_id,),
                )
            results.append(DispatchResult(job_id=job_id, machine_id=task.machine_id, status="pending"))
            tasks_by_type.setdefault(task.machine_type, []).append((task, job_id))

        self._send_to_providers(tasks_by_type, group_id, input_filename, overrides_b64)
        return results

    # ------------------------------------------------------------------
    # Retry on same endpoint
    # ------------------------------------------------------------------

    def retry(self, rj: RenderJob, remaining_start: int, remaining_end: int, error: str, group_id: str) -> str | None:
        step = rj.frame_step or 1
        next_attempt = (rj.attempt or 0) + 1
        max_retries = rj.max_retries or 0

        execute(
            """
            UPDATE jobs
            SET status = 'pending', attempt = %s, rendered_frames = 0,
                error = %s, frame_start = %s
            WHERE id = %s
            """,
            (next_attempt, f"Retry {next_attempt}/{max_retries} (was: {error})", remaining_start, rj.job_id),
        )
        log.warning("Job %s: retry %d/%d on same endpoint", rj.job_id, next_attempt, max_retries)

        overrides_b64 = base64.b64encode((rj.render_overrides_json or "{}").encode()).decode()
        blend_url = self._blend_url_for(rj.machine_type, group_id, rj.input_filename)
        try:
            self._dispatch_one(
                job_id=rj.job_id, machine_id=rj.machine_id, machine_type=rj.machine_type,
                blend_url=blend_url, frame_start=remaining_start, frame_end=remaining_end,
                frame_step=step, overrides_b64=overrides_b64, group_id=group_id,
            )
            return rj.job_id
        except Exception as exc:
            log.error("Same-endpoint retry dispatch failed for %s: %s", rj.job_id, exc)
            return None

    # ------------------------------------------------------------------
    # Failover: mark failed + create new job + dispatch
    # ------------------------------------------------------------------

    def dispatch_failover(
        self, rj: RenderJob, raw_job: dict[str, Any], failover_machine: dict[str, Any],
        remaining_start: int, remaining_end: int, error: str, group_id: str,
    ) -> str:
        step = rj.frame_step or 1
        execute(
            "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
            (now_iso(), f"Failed, migrating remaining frames ({error})", rj.job_id),
        )

        new_job_id = str(uuid4())
        new_total = ((remaining_end - remaining_start) // step) + 1
        machine_type = failover_machine.get("machine_type", "windows")

        execute(
            """
            INSERT INTO jobs (
                id, machine_id, group_id, input_filename, status,
                total_frames, rendered_frames, output_files,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, submitted_at
            )
            VALUES (%s,%s,%s,%s,'pending',%s,0,'[]',%s,%s,%s,%s,0,%s,%s,%s,%s)
            """,
            (
                new_job_id, failover_machine["id"], group_id, raw_job.get("input_filename"),
                new_total, remaining_start, remaining_end, step,
                raw_job.get("render_overrides_json") or "{}",
                raw_job.get("max_retries") or 0, raw_job.get("priority") or 0,
                raw_job.get("chunk_index"), now_iso(),
            ),
        )
        log.info(
            "Job %s -> failover %s on %s (%s)",
            rj.job_id, new_job_id, failover_machine.get("gpu_model", "?"), machine_type,
        )

        overrides_b64 = base64.b64encode((rj.render_overrides_json or "{}").encode()).decode()
        blend_url = self._blend_url_for(machine_type, group_id, raw_job.get("input_filename", ""))
        try:
            self._dispatch_one(
                job_id=new_job_id, machine_id=failover_machine["id"], machine_type=machine_type,
                blend_url=blend_url, frame_start=remaining_start, frame_end=remaining_end,
                frame_step=step, overrides_b64=overrides_b64, group_id=group_id,
            )
        except Exception as exc:
            log.error("Failover dispatch failed for %s: %s", new_job_id, exc)
            execute(
                "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
                (now_iso(), f"Failover dispatch failed: {exc}", new_job_id),
            )
        return new_job_id

    # ------------------------------------------------------------------
    # Status mutations
    # ------------------------------------------------------------------

    def mark_done(self, job_id: str) -> None:
        execute("UPDATE jobs SET status = 'done', completed_at = %s WHERE id = %s", (now_iso(), job_id))

    def mark_failed(self, job_id: str, error: str) -> None:
        execute(
            "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
            (now_iso(), error, job_id),
        )

    # ------------------------------------------------------------------
    # Failover machine selection (was failover_policy.py)
    # ------------------------------------------------------------------

    def pick_failover(self, failed_machine_id: str, failed_machine_type: str) -> dict[str, Any] | None:
        candidates = query_all(
            "SELECT * FROM machines WHERE status = 'available' AND id != %s ORDER BY gpu_vram_gb DESC",
            (failed_machine_id,),
        )
        candidates = filter_enabled_machines(candidates)
        return self._choose_failover(candidates, failed_machine_id, failed_machine_type)

    # ------------------------------------------------------------------
    # Job lookup helpers
    # ------------------------------------------------------------------

    def load_job(self, job: dict[str, Any] | RenderJob) -> tuple[RenderJob, dict[str, Any]] | None:
        """Normalize a job dict or RenderJob into (RenderJob, raw_dict). Returns None if not found."""
        if isinstance(job, RenderJob):
            raw = query_one("SELECT * FROM jobs WHERE id = %s", (job.job_id,))
            if not raw:
                log.error("Job %s not found in DB", job.job_id)
                return None
            return job, raw
        raw = job
        if "machine_type" not in raw:
            row = query_one("SELECT machine_type FROM machines WHERE id = %s", (raw.get("machine_id"),))
            raw = dict(raw)
            raw["machine_type"] = (row or {}).get("machine_type", "windows")
        return RenderJob.from_row(raw), raw

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _choose_failover(
        candidates: list[dict[str, Any]], failed_machine_id: str, failed_machine_type: str,
    ) -> dict[str, Any] | None:
        pool = [c for c in candidates if c["id"] != failed_machine_id]
        if not pool:
            return None

        serverless = [m for m in pool if m.get("machine_type") in SERVERLESS_TYPES]
        non_serverless = [m for m in pool if m.get("machine_type") not in SERVERLESS_TYPES]

        if failed_machine_type == "modal_serverless":
            vast = [m for m in serverless if m.get("machine_type") == "vast_serverless"]
            if vast:
                return vast[0]
            non_modal = [m for m in serverless if m.get("machine_type") != "modal_serverless"]
            if non_modal:
                return non_modal[0]
            return non_serverless[0] if non_serverless else None

        if failed_machine_type == "vast_serverless":
            other_vast = [m for m in serverless if m.get("machine_type") == "vast_serverless"]
            if other_vast:
                return other_vast[0]
            return non_serverless[0] if non_serverless else None

        if serverless:
            vast = [m for m in serverless if m.get("machine_type") == "vast_serverless"]
            return vast[0] if vast else serverless[0]
        return pool[0] if pool else None

    def _dispatch_one(
        self, *, job_id: str, machine_id: str, machine_type: str,
        blend_url: str, frame_start: int, frame_end: int, frame_step: int,
        overrides_b64: str, group_id: str,
    ) -> None:
        strategy = get_strategy(machine_type)
        if not strategy.is_enabled():
            return
        strategy.dispatch(
            job_id=job_id, machine_id=machine_id, blend_url=blend_url,
            frame_start=frame_start, frame_end=frame_end, frame_step=frame_step,
            render_overrides_b64=overrides_b64, group_id=group_id,
        )

    def _send_to_providers(
        self,
        tasks_by_type: dict[str, list[tuple[PlannedTask, str]]],
        group_id: str,
        input_filename: str,
        overrides_b64: str,
    ) -> None:
        for machine_type, task_pairs in tasks_by_type.items():
            strategy = get_strategy(machine_type)
            if not strategy.is_enabled():
                continue
            blend_url = self._blend_url_for(machine_type, group_id, input_filename)

            if machine_type == "modal_serverless":
                self._send_modal(task_pairs, blend_url, overrides_b64, group_id)
            elif machine_type == "vast_serverless":
                self._send_vast(task_pairs, blend_url, overrides_b64, group_id)
            else:
                for task, job_id in task_pairs:
                    try:
                        self._dispatch_one(
                            job_id=job_id, machine_id=task.machine_id, machine_type=task.machine_type,
                            blend_url=blend_url, frame_start=task.frame_start, frame_end=task.frame_end,
                            frame_step=task.frame_step, overrides_b64=overrides_b64, group_id=group_id,
                        )
                    except Exception as exc:
                        log.error("Dispatch failed for %s: %s", job_id, exc)

    def _send_modal(self, task_pairs: list[tuple[PlannedTask, str]], blend_url: str, overrides_b64: str, group_id: str) -> None:
        def _do(task: PlannedTask, jid: str) -> None:
            try:
                self._dispatch_one(
                    job_id=jid, machine_id=task.machine_id, machine_type="modal_serverless",
                    blend_url=blend_url, frame_start=task.frame_start, frame_end=task.frame_end,
                    frame_step=task.frame_step, overrides_b64=overrides_b64, group_id=group_id,
                )
            except Exception as exc:
                log.error("Modal dispatch failed for %s: %s", jid, exc)
                execute(
                    "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                    (f"Dispatch failed: {exc}", now_iso(), jid),
                )
        for i, (task, job_id) in enumerate(task_pairs):
            if i > 0:
                time.sleep(0.05)
            threading.Thread(target=_do, args=(task, job_id), daemon=True, name=f"modal-dispatch-{job_id[:8]}").start()

    def _send_vast(self, task_pairs: list[tuple[PlannedTask, str]], blend_url: str, overrides_b64: str, group_id: str) -> None:
        def _sequential() -> None:
            for task, job_id in task_pairs:
                try:
                    self._dispatch_one(
                        job_id=job_id, machine_id=task.machine_id, machine_type="vast_serverless",
                        blend_url=blend_url, frame_start=task.frame_start, frame_end=task.frame_end,
                        frame_step=task.frame_step, overrides_b64=overrides_b64, group_id=group_id,
                    )
                except Exception as exc:
                    log.error("Vast dispatch failed for %s: %s", job_id, exc)
                    execute(
                        "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                        (f"Dispatch failed: {exc}", now_iso(), job_id),
                    )
                time.sleep(2)
        threading.Thread(target=_sequential, daemon=True, name=f"vast-dispatch-group-{group_id[:8]}").start()

    @staticmethod
    def _blend_url_for(machine_type: str, group_id: str, input_filename: str) -> str:
        if machine_type == "modal_serverless":
            from services import modal as provider
        else:
            from services import vast as provider
        return f"{provider.PUBLIC_BACKEND_URL}/render-groups/{group_id}/input/{input_filename}"


fleet = FleetManager()
