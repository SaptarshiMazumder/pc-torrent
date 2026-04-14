"""Modal serverless polling loop (state-machine style).

Tracks each Modal job with provider-aware polling so status transitions are as
strict as the Vast poller: provider status, stale detection, completion salvage,
and explicit failover path.
"""

from __future__ import annotations

import logging
import threading
import time

from models.value_objects import now_iso, parse_output_files
from infrastructure.db import execute, query_one
from services.modal.config import ModalConfig
from services.modal.instance_registry import MAX_LOG_LINES
from services.modal.instance_registry import registry as _registry
from services.modal.job_state import function_call_id_from_job
from services.modal.provider_status import (
    ModalCallSnapshot,
    get_function_call_snapshot,
)

log = logging.getLogger(__name__)

_JOB_QUERY = """
    SELECT status, attempt, max_retries, frame_start, frame_end,
           frame_step, rendered_frames, total_frames, output_files, input_filename,
           render_overrides_json, chunk_index, priority,
           modal_function_call_id, last_heartbeat_at
    FROM jobs WHERE id = %s
"""

_HEARTBEAT_GRACE_SEC = 90    # time after job starts before heartbeat is required
_HEARTBEAT_TIMEOUT_SEC = 45  # max age of last heartbeat before considered dead

_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
_MIN_PROVIDER_FAILURE_GRACE_SEC = 30


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

        self._started_at = time.monotonic()
        self._poll_count = 0
        self._last_activity_marker: tuple[int, int] | None = None
        self._last_activity_at = time.monotonic()
        self._last_progress_at: float | None = None
        self._prev_provider_status: str | None = None
        self._prev_job_status: str | None = None

    def start(self) -> None:
        _registry.set(
            self._job_id,
            {
                "job_id": self._job_id,
                "provider_job_id": self._provider_job_id,
                "machine_id": self._machine_id,
                "group_id": self._group_id,
                "gpu_type": self._gpu_type(),
                "job_status": "pending",
                "provider_status": None,
                "provider_function": None,
                "provider_raw_statuses": [],
                "rendered_frames": 0,
                "output_count": 0,
                "total_frames": None,
                "frame_start": None,
                "frame_end": None,
                "elapsed_sec": 0,
                "started_at": now_iso(),
                "last_poll_at": now_iso(),
                "monitor_action": "started",
                "error": None,
                "status_history": [{"status": "started", "at": now_iso()}],
            },
        )
        t = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"modal-mon-{self._job_id[:8]}",
        )
        t.start()

    def _gpu_type(self) -> str:
        try:
            from services.modal.machine_registrar import MachineRegistrar

            reg = MachineRegistrar(self._cfg)
            return reg.gpu_type_for_machine(self._machine_id)
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            time.sleep(self._cfg.monitor_interval_sec)
            self._poll_count += 1
            try:
                if self._tick():
                    break
            except Exception as exc:
                log.error("Modal poll error for job %s: %s", self._job_id, exc)
                self._reg_update(
                    {
                        "monitor_action": f"error:{exc}",
                        "last_poll_at": now_iso(),
                    }
                )

    def _tick(self) -> bool:
        job = query_one(_JOB_QUERY, (self._job_id,))
        elapsed = time.monotonic() - self._started_at

        if not job:
            self._reg_update(
                {
                    "job_status": "gone",
                    "monitor_action": "job_not_found",
                    "last_poll_at": now_iso(),
                    "elapsed_sec": int(elapsed),
                }
            )
            _registry.remove(self._job_id)
            return True

        local_status = str(job.get("status") or "")
        output_count = self._output_count(job)
        provider_job_id = function_call_id_from_job(job) or self._provider_job_id
        provider = self._poll_provider(local_status, provider_job_id)

        self._update_activity(job, output_count)
        self._update_registry(
            job=job,
            local_status=local_status,
            output_count=output_count,
            provider_job_id=provider_job_id,
            provider=provider,
            elapsed=elapsed,
        )

        if local_status in _TERMINAL_STATUSES:
            self._reg_update({"monitor_action": f"db_terminal:{local_status}"})
            _registry.remove(self._job_id)
            return True

        if self._is_complete(job, output_count):
            self._mark_done(
                job,
                output_count=output_count,
                reason="all expected outputs are already registered",
            )
            self._reg_update({"monitor_action": "complete_from_outputs"})
            _registry.remove(self._job_id)
            return True

        if provider and provider.is_success:
            self._mark_done(
                job,
                output_count=output_count,
                reason=f"Modal provider reported {provider.status}",
            )
            self._reg_update({"monitor_action": f"provider_success:{provider.status}"})
            _registry.remove(self._job_id)
            return True

        if local_status == "pending" and provider and provider.is_running:
            execute(
                "UPDATE jobs SET status = 'running' WHERE id = %s AND status = 'pending'",
                (self._job_id,),
            )
            self._reg_update({"monitor_action": "auto_transition:provider_running"})
            local_status = "running"

        if provider and provider.is_failure:
            if self._has_recent_progress():
                age = int(time.monotonic() - (self._last_progress_at or time.monotonic()))
                self._reg_update(
                    {
                        "monitor_action": (
                            f"provider_failure_deferred:{provider.status} "
                            f"(progress {age}s ago)"
                        )
                    }
                )
            else:
                error = f"Modal function call ended with {provider.status}"
                failure_applied = self._handle_failure(job, error)
                if failure_applied:
                    self._reg_update(
                        {
                            "monitor_action": f"provider_failure:{provider.status}",
                            "error": error,
                        }
                    )
                else:
                    self._reg_update(
                        {"monitor_action": "completion_before_provider_failure"}
                    )
                _registry.remove(self._job_id)
                return True

        if local_status == "pending" and elapsed > self._cfg.in_queue_timeout_sec:
            if not (provider and provider.is_running):
                error = f"Modal job stuck in pending for {elapsed:.0f}s - routing to failover"
                failure_applied = self._handle_failure(job, error)
                if failure_applied:
                    self._reg_update(
                        {
                            "monitor_action": f"pending_timeout:{elapsed:.0f}s",
                            "error": error,
                        }
                    )
                else:
                    self._reg_update({"monitor_action": "completion_before_pending_timeout"})
                _registry.remove(self._job_id)
                return True

        if local_status == "running" and self._is_heartbeat_dead(job):
            error = (
                f"Modal job heartbeat dead (no ping for >{_HEARTBEAT_TIMEOUT_SEC}s) - "
                "container likely cancelled or crashed"
            )
            failure_applied = self._handle_failure(job, error)
            if failure_applied:
                self._reg_update({"monitor_action": "heartbeat_dead", "error": error})
            else:
                self._reg_update({"monitor_action": "completion_before_heartbeat_dead"})
            _registry.remove(self._job_id)
            return True

        if local_status == "running" and self._is_stale():
            error = (
                "Modal job running but no new frames or outputs for "
                f"{self._cfg.in_progress_stale_sec / 60:.0f} min - cancelling"
            )
            failure_applied = self._handle_failure(job, error)
            if failure_applied:
                self._reg_update(
                    {
                        "monitor_action": (
                            f"stale_progress:{self._cfg.in_progress_stale_sec / 60:.0f}min"
                        ),
                        "error": error,
                    }
                )
            else:
                self._reg_update({"monitor_action": "completion_before_stale_timeout"})
            _registry.remove(self._job_id)
            return True

        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _poll_provider(
        self, local_status: str, provider_job_id: str
    ) -> ModalCallSnapshot | None:
        # Some Modal SDK versions expose get_logs() as a streaming iterator that
        # can block. Keep provider polling non-blocking; rely on status graph.
        include_logs = False
        _ = local_status
        _ = self._poll_count
        return get_function_call_snapshot(
            provider_job_id,
            include_logs=include_logs,
            max_log_lines=MAX_LOG_LINES,
        )

    def _update_activity(self, job: dict, output_count: int) -> None:
        marker = (job.get("rendered_frames") or 0, output_count)
        if marker != self._last_activity_marker:
            self._last_activity_marker = marker
            self._last_activity_at = time.monotonic()
            if marker != (0, 0):
                self._last_progress_at = time.monotonic()

    def _update_registry(
        self,
        *,
        job: dict,
        local_status: str,
        output_count: int,
        provider_job_id: str,
        provider: ModalCallSnapshot | None,
        elapsed: float,
    ) -> None:
        provider_status = provider.status if provider else None
        provider_logs = provider.logs if provider else ""

        updates: dict = {
            "job_status": local_status,
            "provider_status": provider_status,
            "provider_job_id": provider_job_id,
            "provider_function": provider.function_name if provider else None,
            "provider_raw_statuses": list(provider.raw_statuses) if provider else [],
            "rendered_frames": job.get("rendered_frames") or 0,
            "output_count": output_count,
            "total_frames": job.get("total_frames"),
            "frame_start": job.get("frame_start"),
            "frame_end": job.get("frame_end"),
            "elapsed_sec": int(elapsed),
            "last_poll_at": now_iso(),
            "error": job.get("error") if local_status == "failed" else None,
        }
        if provider_logs:
            updates["logs"] = provider_logs
        _registry.set(self._job_id, updates)

        if local_status != self._prev_job_status:
            _registry.append_history(
                self._job_id, {"status": f"job:{local_status}", "at": now_iso()}
            )
            self._prev_job_status = local_status

        if provider_status and provider_status != self._prev_provider_status:
            _registry.append_history(
                self._job_id, {"status": f"provider:{provider_status}", "at": now_iso()}
            )
            self._prev_provider_status = provider_status

    def _reg_update(self, updates: dict) -> None:
        _registry.set(self._job_id, updates)
        action = updates.get("monitor_action")
        if action:
            _registry.append_history(self._job_id, {"status": action, "at": now_iso()})

    def _is_heartbeat_dead(self, job: dict) -> bool:
        """Return True if the container has stopped sending heartbeats.

        Mirrors the Vast poller's logic: ignore until the grace period has
        elapsed, then require a heartbeat no older than _HEARTBEAT_TIMEOUT_SEC.
        """
        elapsed = time.monotonic() - self._started_at
        if elapsed < _HEARTBEAT_GRACE_SEC:
            return False  # still within cold-start window

        last_hb = job.get("last_heartbeat_at")
        if last_hb is None:
            # Never sent a heartbeat after the grace period — container is dead.
            return True

        try:
            from datetime import datetime, timezone
            hb_time = datetime.fromisoformat(str(last_hb).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - hb_time).total_seconds()
            return age > _HEARTBEAT_TIMEOUT_SEC
        except Exception:
            return False

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._last_activity_at) > self._cfg.in_progress_stale_sec

    def _has_recent_progress(self) -> bool:
        if self._last_progress_at is None:
            return False
        grace_sec = max(
            _MIN_PROVIDER_FAILURE_GRACE_SEC,
            min(self._cfg.in_progress_stale_sec / 3, 10 * 60),
        )
        return (time.monotonic() - self._last_progress_at) < grace_sec

    def _handle_failure(self, job: dict, error: str) -> bool:
        from scheduling.orchestrator import orchestrator
        from services import modal

        output_count = self._output_count(job)
        if self._is_complete(job, output_count):
            self._mark_done(
                job,
                output_count=output_count,
                reason="completion evidence detected before failover",
            )
            return False

        log.warning("Job %s: %s", self._job_id, error)
        provider_job_id = function_call_id_from_job(job) or self._provider_job_id
        if provider_job_id:
            try:
                modal.cancel_job(provider_job_id, self._machine_id)
            except Exception as exc:
                log.warning(
                    "Job %s: failed to cancel Modal call %s: %s",
                    self._job_id,
                    provider_job_id,
                    exc,
                )
        orchestrator.handle_failure(
            job=job,
            error=error,
            group_id=self._group_id,
        )
        return True

    def _output_count(self, job: dict) -> int:
        return len(parse_output_files(job.get("output_files")))

    def _is_complete(self, job: dict, output_count: int) -> bool:
        total_frames = job.get("total_frames") or 0
        if total_frames <= 0:
            return False
        rendered_frames = job.get("rendered_frames") or 0
        return rendered_frames >= total_frames or output_count >= total_frames

    def _mark_done(self, job: dict, *, output_count: int, reason: str) -> None:
        total_frames = job.get("total_frames") or 0
        rendered_frames = max(
            job.get("rendered_frames") or 0,
            min(total_frames, output_count),
        )
        execute(
            """
            UPDATE jobs
            SET status = 'done', completed_at = %s, error = NULL, rendered_frames = %s
            WHERE id = %s AND status NOT IN ('done', 'cancelled')
            """,
            (now_iso(), max(total_frames, rendered_frames), self._job_id),
        )
        log.info("Job %s marked done by Modal monitor: %s", self._job_id, reason)
