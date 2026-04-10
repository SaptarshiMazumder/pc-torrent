"""
InstancePoller — poll loop rewritten as a state machine.

Each _on_* handler is a single clear transition (5–15 lines).
The old 250-line nested-if _poll() is replaced by a flat _tick() that
delegates to the appropriate handler based on instance/job state.
"""

from __future__ import annotations

import logging
import threading
import time

from domain.value_objects import now_iso
from infrastructure.db import execute, query_one
from services.vast.client import VastApiClient
from services.vast.config import VastConfig
from services.vast.failover import FailoverHandler
from services.vast.instance_registry import InstanceRegistry, MAX_LOG_LINES

log = logging.getLogger(__name__)

_JOB_QUERY = """
    SELECT status, attempt, max_retries, frame_start, frame_end, frame_step,
           rendered_frames, total_frames, input_filename, render_overrides_json,
           chunk_index, chunk_size_frames, priority, error
    FROM jobs WHERE id = %s
"""

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

LOG_FETCH_EVERY = 2
EXIT_CALLBACK_WAIT_SEC = 60
EXIT_POLL_SEC = 5


class InstancePoller:
    def __init__(
        self,
        job_id: str,
        instance_id: int,
        machine_id: str,
        blend_url: str,
        render_overrides_b64: str,
        group_id: str,
        client: VastApiClient,
        registry: InstanceRegistry,
        failover: FailoverHandler,
        config: VastConfig,
    ) -> None:
        self._job_id = job_id
        self._instance_id = instance_id
        self._machine_id = machine_id
        self._blend_url = blend_url
        self._render_overrides_b64 = render_overrides_b64
        self._group_id = group_id
        self._client = client
        self._registry = registry
        self._failover = failover
        self._cfg = config

        self._started_at = time.monotonic()
        self._last_rendered_frames: int | None = None
        self._last_frame_change_at = time.monotonic()
        self._became_running_at: float | None = None
        self._prev_actual_status = "created"
        self._poll_count = 0

    def start(self) -> None:
        self._registry.set(self._job_id, {
            "job_id": self._job_id,
            "instance_id": self._instance_id,
            "machine_id": self._machine_id,
            "group_id": self._group_id,
            "actual_status": "created",
            "job_status": "pending",
            "gpu_model": None,
            "dph_total": None,
            "rendered_frames": 0,
            "total_frames": None,
            "frame_start": None,
            "frame_end": None,
            "elapsed_sec": 0,
            "started_at": now_iso(),
            "last_poll_at": now_iso(),
            "logs": "",
            "error": None,
            "status_history": [{"status": "created", "at": now_iso()}],
        })

        t = threading.Thread(
            target=self._run, daemon=True,
            name=f"vast-poll-{self._job_id[:8]}",
        )
        t.start()

    def _run(self) -> None:
        while True:
            time.sleep(self._cfg.poll_interval_sec)
            self._poll_count += 1
            try:
                if self._tick():
                    break
            except Exception as e:
                log.error(f"Vast.ai poll error for job {self._job_id}: {e}")
                self._registry.set(self._job_id, {
                    "last_poll_at": now_iso(),
                    "poll_error": str(e),
                })

    def _tick(self) -> bool:
        """Single poll iteration. Returns True when polling should stop."""
        inst = self._client.get_instance(self._instance_id)
        elapsed = time.monotonic() - self._started_at

        if inst is None:
            return self._on_instance_gone(elapsed)

        actual_status = str(inst.get("actual_status") or "").lower()
        status_msg = str(inst.get("status_msg") or "").lower()

        if self._has_fatal_startup_error(actual_status, status_msg):
            return self._on_fatal_error(inst)

        job = query_one(_JOB_QUERY, (self._job_id,))
        if not job:
            log.warning(f"Poll: job {self._job_id} not found in DB, stopping")
            self._client.destroy_instance(self._instance_id)
            self._registry.remove(self._job_id)
            return True

        self._update_registry(inst, job, actual_status, elapsed)

        local_status = job["status"]

        if local_status in ("done", "failed", "cancelled"):
            return self._on_terminal_callback(local_status)

        if self._all_frames_done(job, actual_status, local_status):
            return self._on_proactive_complete(job)

        if (self._became_running_at is None
                and actual_status not in ("running",)
                and elapsed > self._cfg.startup_timeout_sec):
            return self._on_startup_timeout(actual_status, elapsed, job)

        if actual_status == "running":
            self._on_became_running(job)
            if self._is_stale(job):
                return self._on_stale(job)

        if actual_status in ("exited", "stopped", "offline"):
            return self._on_exited(inst, actual_status, job)

        return False

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _on_instance_gone(self, elapsed: float) -> bool:
        job = query_one(_JOB_QUERY, (self._job_id,))
        local_status = job["status"] if job else "unknown"
        self._registry.set(self._job_id, {
            "actual_status": "gone",
            "job_status": local_status,
            "elapsed_sec": int(elapsed),
            "last_poll_at": now_iso(),
        })
        if local_status in ("done", "failed", "cancelled"):
            log.info(f"Vast.ai instance {self._instance_id} gone; job {self._job_id} is {local_status}")
        elif job and (job.get("total_frames") or 0) > 0 and (job.get("rendered_frames") or 0) >= (job.get("total_frames") or 0):
            log.info(
                f"Vast.ai instance {self._instance_id} gone but all "
                f"{job['total_frames']} frames rendered — marking done"
            )
            execute(
                "UPDATE jobs SET status = 'done', completed_at = %s, "
                "rendered_frames = %s WHERE id = %s",
                (now_iso(), job["total_frames"], self._job_id),
            )
        else:
            log.warning(
                f"Vast.ai instance {self._instance_id} not found; "
                f"job {self._job_id}={local_status}, failing over"
            )
            if job:
                self._failover.handle(
                    job_id=self._job_id, job=job,
                    error="Vast.ai instance disappeared unexpectedly",
                    blend_url=self._blend_url,
                    render_overrides_b64=self._render_overrides_b64,
                    failed_machine_id=self._machine_id,
                    group_id=self._group_id,
                )
        self._registry.remove(self._job_id)
        return True

    def _on_fatal_error(self, inst: dict) -> bool:
        fatal_err = (
            f"Vast.ai instance {self._instance_id} fatal startup error "
            f"(status={inst.get('actual_status')}): "
            f"{str(inst.get('status_msg', ''))[:200]}"
        )
        log.warning(f"Job {self._job_id}: {fatal_err}")
        self._registry.set(self._job_id, {"error": fatal_err, "actual_status": "error"})
        self._client.destroy_instance(self._instance_id)
        job = query_one(_JOB_QUERY, (self._job_id,))
        if job:
            self._failover.handle(
                job_id=self._job_id, job=job, error=fatal_err,
                blend_url=self._blend_url,
                render_overrides_b64=self._render_overrides_b64,
                failed_machine_id=self._machine_id,
                group_id=self._group_id,
            )
        self._registry.remove(self._job_id)
        return True

    def _on_terminal_callback(self, local_status: str) -> bool:
        log.info(
            f"Job {self._job_id} is {local_status}; "
            f"destroying Vast.ai instance {self._instance_id}"
        )
        self._client.destroy_instance(self._instance_id)
        self._registry.remove(self._job_id)
        return True

    def _on_proactive_complete(self, job: dict) -> bool:
        total = job.get("total_frames") or 0
        log.info(
            f"Job {self._job_id}: all {total} frames rendered, "
            f"marking done and destroying instance"
        )
        execute(
            "UPDATE jobs SET status = 'done', completed_at = %s, rendered_frames = %s WHERE id = %s",
            (now_iso(), total, self._job_id),
        )
        self._client.destroy_instance(self._instance_id)
        self._registry.remove(self._job_id)
        return True

    def _on_startup_timeout(self, actual_status: str, elapsed: float, job: dict) -> bool:
        err = (
            f"Vast.ai instance {self._instance_id} stuck in '{actual_status}' "
            f"for {elapsed:.0f}s — timing out"
        )
        log.warning(f"Job {self._job_id}: {err}")
        self._registry.set(self._job_id, {"error": err})
        self._client.destroy_instance(self._instance_id)
        self._failover.handle(
            job_id=self._job_id, job=job, error=err,
            blend_url=self._blend_url,
            render_overrides_b64=self._render_overrides_b64,
            failed_machine_id=self._machine_id,
            group_id=self._group_id,
        )
        self._registry.remove(self._job_id)
        return True

    def _on_became_running(self, job: dict) -> None:
        if self._became_running_at is not None:
            return
        self._became_running_at = time.monotonic()
        log.info(f"Vast.ai instance {self._instance_id} running for job {self._job_id}")
        if job["status"] == "pending":
            execute(
                "UPDATE jobs SET status = 'running' WHERE id = %s AND status = 'pending'",
                (self._job_id,),
            )
            log.info(f"Job {self._job_id}: auto-transitioned pending → running")

    def _on_stale(self, job: dict) -> bool:
        err = (
            f"Vast.ai job running but no new frames for "
            f"{self._cfg.in_progress_stale_sec / 60:.0f} min — cancelling"
        )
        log.warning(f"Job {self._job_id}: {err}")
        self._registry.set(self._job_id, {"error": err})
        self._client.destroy_instance(self._instance_id)
        self._failover.handle(
            job_id=self._job_id, job=job, error=err,
            blend_url=self._blend_url,
            render_overrides_b64=self._render_overrides_b64,
            failed_machine_id=self._machine_id,
            group_id=self._group_id,
        )
        self._registry.remove(self._job_id)
        return True

    def _on_exited(self, inst: dict, actual_status: str, job: dict) -> bool:
        exit_code = inst.get("exit_code")
        log.info(
            f"Vast.ai instance {self._instance_id} exited "
            f"(status={actual_status}, exit_code={exit_code}) for job {self._job_id}"
        )
        self._registry.set(self._job_id, {
            "actual_status": actual_status,
            "exit_code": exit_code,
            "last_poll_at": now_iso(),
        })
        self._client.destroy_instance(self._instance_id)

        rendered = job.get("rendered_frames") or 0
        total = job.get("total_frames") or 0
        if total > 0 and rendered >= total:
            log.info(
                f"Job {self._job_id}: all {total} frames rendered before exit, "
                f"marking done (no callback needed)"
            )
            execute(
                "UPDATE jobs SET status = 'done', completed_at = %s, "
                "rendered_frames = %s WHERE id = %s",
                (now_iso(), total, self._job_id),
            )
            self._registry.remove(self._job_id)
            return True

        local_status = self._wait_for_callback()

        if local_status in ("done", "failed", "cancelled"):
            log.info(f"Job {self._job_id} is {local_status} after instance exit — OK")
        else:
            err = (
                f"Vast.ai instance exited (status={actual_status}, "
                f"exit_code={exit_code}) — callback did not arrive within "
                f"{EXIT_CALLBACK_WAIT_SEC}s"
            )
            log.warning(f"Job {self._job_id}: {err}")
            self._registry.set(self._job_id, {"error": err})
            job_now = query_one(_JOB_QUERY, (self._job_id,))
            if job_now:
                self._failover.handle(
                    job_id=self._job_id, job=job_now, error=err,
                    blend_url=self._blend_url,
                    render_overrides_b64=self._render_overrides_b64,
                    failed_machine_id=self._machine_id,
                    group_id=self._group_id,
                )

        self._registry.remove(self._job_id)
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _has_fatal_startup_error(self, actual_status: str, status_msg: str) -> bool:
        return (
            actual_status not in ("running", "exited", "stopped", "offline")
            and any(frag in status_msg for frag in _FATAL_MSG_FRAGMENTS)
        )

    def _all_frames_done(self, job: dict, actual_status: str, local_status: str) -> bool:
        if local_status != "running":
            return False
        rendered = job.get("rendered_frames") or 0
        total = job.get("total_frames") or 0
        return total > 0 and rendered >= total

    def _is_stale(self, job: dict) -> bool:
        if job["status"] != "running":
            return False
        cur_frames = job.get("rendered_frames") or 0
        if cur_frames != self._last_rendered_frames:
            self._last_rendered_frames = cur_frames
            self._last_frame_change_at = time.monotonic()
            return False
        return time.monotonic() - self._last_frame_change_at > self._cfg.in_progress_stale_sec

    def _update_registry(
        self, inst: dict, job: dict, actual_status: str, elapsed: float,
    ) -> None:
        logs = ""
        if actual_status != "running" or self._poll_count % LOG_FETCH_EVERY == 0:
            logs = self._client.get_logs(self._instance_id)

        if actual_status != self._prev_actual_status:
            self._registry.append_history(
                self._job_id,
                {"status": actual_status, "at": now_iso()},
            )
            self._prev_actual_status = actual_status

        local_status = job["status"]
        gpu_ram_mb = inst.get("gpu_ram") or 0
        gpu_vram_gb = round(gpu_ram_mb / 1024, 1) if gpu_ram_mb > 100 else gpu_ram_mb
        update: dict = {
            "actual_status": actual_status,
            "job_status": local_status,
            "gpu_model": inst.get("gpu_name") or inst.get("gpu_display_model"),
            "gpu_vram_gb": gpu_vram_gb,
            "dph_total": inst.get("dph_total"),
            "rendered_frames": job.get("rendered_frames") or 0,
            "total_frames": job.get("total_frames"),
            "frame_start": job.get("frame_start"),
            "frame_end": job.get("frame_end"),
            "elapsed_sec": int(elapsed),
            "last_poll_at": now_iso(),
            "status_msg": inst.get("status_msg") or "",
            "error": job.get("error") if local_status == "failed" else None,
        }
        if logs:
            update["logs"] = "\n".join(logs.splitlines()[-MAX_LOG_LINES:])
        self._registry.set(self._job_id, update)

    def _wait_for_callback(self) -> str:
        """Wait up to EXIT_CALLBACK_WAIT_SEC for the worker callback to arrive."""
        iterations = max(1, EXIT_CALLBACK_WAIT_SEC // EXIT_POLL_SEC)
        local_status = "unknown"
        for _ in range(iterations):
            time.sleep(EXIT_POLL_SEC)
            job = query_one(_JOB_QUERY, (self._job_id,))
            local_status = job["status"] if job else "unknown"
            self._registry.set(self._job_id, {
                "job_status": local_status,
                "last_poll_at": now_iso(),
            })
            if local_status in ("done", "failed", "cancelled"):
                break
            log.debug(
                f"Job {self._job_id} still '{local_status}' after instance exit, "
                f"waiting for callback..."
            )
        return local_status
