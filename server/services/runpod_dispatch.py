"""
RunPod serverless dispatch layer (multi-endpoint).

Responsibilities:
- Register/maintain a virtual "runpod_serverless" machine per endpoint
- Keep heartbeats alive so they stay visible in the available machines list
- Dispatch render jobs to the correct RunPod endpoint
- Poll RunPod /status/{id} until COMPLETED or FAILED, then delegate
  retry/failover to DispatchCoordinator

Env var formats (pick one):
  Multi-endpoint:  RUNPOD_ENDPOINTS=id1:Label One,id2:Label Two
  Single (legacy): RUNPOD_ENDPOINT_ID=id1
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import base64

import httpx

from domain.value_objects import now_iso
from infrastructure.db import execute, query_all, query_one
from services import runpod_autoscaler

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env parsing helpers
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(f"Invalid {name}='{raw}', using default {default}")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(f"Invalid {name}='{raw}', using default {default}")
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    log.warning(f"Invalid {name}='{raw}', using default {default}")
    return default


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")

RUNPOD_GPU_VRAM_GB = _env_float("RUNPOD_GPU_VRAM_GB", 24.0)
RUNPOD_CPU_CORES = _env_int("RUNPOD_CPU_CORES", 16)
RUNPOD_RAM_GB = _env_float("RUNPOD_RAM_GB", 64.0)

PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000")

JOB_STATUS_POLL_INTERVAL_SEC = float(os.getenv("JOB_STATUS_POLL_INTERVAL_SEC", "5"))
IN_QUEUE_TIMEOUT_SEC = _env_float("IN_QUEUE_TIMEOUT_SEC", 120)
RUNPOD_KILL_THROTTLED_IMMEDIATELY = _env_bool("RUNPOD_KILL_THROTTLED_IMMEDIATELY", True)
IN_PROGRESS_STALE_SEC = _env_float("IN_PROGRESS_STALE_SEC", 90 * 60)
RUNPOD_INIT_STALL_SEC = _env_float("RUNPOD_INIT_STALL_SEC", 120)

_HEARTBEAT_INTERVAL = 10


# ---------------------------------------------------------------------------
# Endpoint parsing
# ---------------------------------------------------------------------------

def _parse_endpoints() -> list[dict]:
    raw = os.getenv("RUNPOD_ENDPOINTS", "").strip()
    if raw:
        endpoints = []
        base_label = os.getenv("RUNPOD_GPU_MODEL", "RunPod Serverless")
        for i, part in enumerate(raw.split(","), 1):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                eid, label = part.split(":", 1)
                endpoints.append({"id": eid.strip(), "label": label.strip()})
            else:
                endpoints.append({"id": part, "label": f"{base_label} #{i}"})
        return endpoints

    eid = os.getenv("RUNPOD_ENDPOINT_ID", "").strip()
    if eid:
        label = os.getenv("RUNPOD_GPU_MODEL", "RunPod Serverless (RTX 4090)")
        return [{"id": eid, "label": label}]
    return []


ENDPOINTS = _parse_endpoints()

# machine_id -> endpoint_id
_machine_endpoint_map: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return bool(RUNPOD_API_KEY and ENDPOINTS)


def _deactivate_stale_virtual_machines(active_machine_keys: list[str]) -> None:
    if not active_machine_keys:
        execute("UPDATE machines SET status = 'idle' WHERE machine_type = 'runpod_serverless'")
        return
    placeholders = ", ".join(["%s"] * len(active_machine_keys))
    execute(
        f"""
        UPDATE machines
        SET status = 'idle'
        WHERE machine_type = 'runpod_serverless'
          AND machine_key NOT IN ({placeholders})
        """,
        tuple(active_machine_keys),
    )


def register_virtual_machines() -> list[str]:
    """Upsert one virtual machine row per RunPod endpoint."""
    if not is_enabled():
        _machine_endpoint_map.clear()
        _deactivate_stale_virtual_machines([])
        log.info("RunPod not configured — skipping registration")
        return []

    _machine_endpoint_map.clear()
    machine_ids = []
    active_machine_keys = []
    now = now_iso()

    for ep in ENDPOINTS:
        endpoint_id = ep["id"]
        label = ep["label"]
        machine_key = f"runpod-serverless-{endpoint_id}"
        active_machine_keys.append(machine_key)

        existing = query_one("SELECT id FROM machines WHERE machine_key = %s", (machine_key,))
        if existing:
            machine_id = existing["id"]
            execute(
                """
                UPDATE machines
                SET gpu_model = %s, gpu_vram_gb = %s, cpu_cores = %s, ram_gb = %s,
                    os_version = %s, machine_type = %s,
                    status = 'available', last_seen_at = %s
                WHERE id = %s
                """,
                (label, RUNPOD_GPU_VRAM_GB, RUNPOD_CPU_CORES, RUNPOD_RAM_GB,
                 "Linux", "runpod_serverless", now, machine_id),
            )
            log.info(f"RunPod endpoint {endpoint_id} updated: {machine_id} ({label})")
        else:
            machine_id = str(uuid4())
            execute(
                """
                INSERT INTO machines (
                    id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb,
                    os_version, machine_type, status, registered_at, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'available', %s, %s)
                """,
                (machine_id, machine_key,
                 label, RUNPOD_GPU_VRAM_GB, RUNPOD_CPU_CORES, RUNPOD_RAM_GB,
                 "Linux", "runpod_serverless", now, now),
            )
            log.info(f"RunPod endpoint {endpoint_id} registered: {machine_id} ({label})")

        _machine_endpoint_map[machine_id] = endpoint_id
        machine_ids.append(machine_id)

    _deactivate_stale_virtual_machines(active_machine_keys)
    return machine_ids


def start_heartbeat_thread() -> None:
    if not is_enabled():
        return

    machine_keys = [f"runpod-serverless-{ep['id']}" for ep in ENDPOINTS]

    def _loop():
        while True:
            try:
                for mk in machine_keys:
                    execute(
                        """
                        UPDATE machines
                        SET last_seen_at = %s
                        WHERE machine_key = %s AND machine_type = 'runpod_serverless'
                        """,
                        (now_iso(), mk),
                    )
            except Exception as e:
                log.warning(f"RunPod heartbeat error: {e}")
            time.sleep(_HEARTBEAT_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="runpod-heartbeat")
    t.start()
    log.info(f"RunPod heartbeat thread started ({len(ENDPOINTS)} endpoint(s))")


def _endpoint_id_for_machine(machine_id: str) -> str:
    eid = _machine_endpoint_map.get(machine_id)
    if eid:
        return eid
    if len(ENDPOINTS) == 1:
        return ENDPOINTS[0]["id"]
    raise ValueError(f"No RunPod endpoint mapped for machine_id={machine_id}")


def endpoint_id_for_machine(machine_id: str) -> str:
    return _endpoint_id_for_machine(machine_id)


def _parse_iso8601(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _heartbeat_age_seconds(raw: str | None) -> float | None:
    dt = _parse_iso8601(raw)
    if not dt:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def _classify_failure_type(error: str, default: str = "crash") -> str:
    low = (error or "").lower()
    if "throttl" in low:
        return "throttle"
    if "stuck in queue" in low or "queue timeout" in low:
        return "queue_timeout"
    if "initializing" in low or "rendered_frames still 0" in low or "heartbeat stopped" in low:
        return "init_stall"
    if "no new frames" in low or "stale" in low:
        return "stale_render"
    if "cancel" in low:
        return "cancelled"
    if "out of memory" in low or "oom" in low:
        return "oom"
    return default


def _is_infrastructure_failure(failure_type: str) -> bool:
    return failure_type in {"queue_timeout", "init_stall", "throttle"}


def _render_overrides_b64(job: dict) -> str:
    return base64.b64encode((job.get("render_overrides_json") or "{}").encode()).decode()


def _blend_url_for_group(group_id: str, db_query_one) -> str:
    if not group_id:
        return ""
    group = db_query_one(
        "SELECT input_filename FROM render_groups WHERE id = %s",
        (group_id,),
    )
    if not group:
        return ""
    return f"{PUBLIC_BACKEND_URL}/render-groups/{group_id}/input/{group['input_filename']}"


def _reassigned_endpoint_label(machine_row: dict, machine_type: str) -> str:
    if machine_type == "runpod_serverless":
        try:
            return _endpoint_id_for_machine(machine_row["id"])
        except Exception:
            return machine_row.get("machine_key") or machine_row["id"]
    return machine_row.get("machine_key") or machine_row["id"]


def dispatch_job(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
    machine_id: str = "",
) -> str:
    """POST a job to the correct RunPod endpoint. Returns the RunPod job ID."""
    endpoint_id = _endpoint_id_for_machine(machine_id)

    resp = httpx.post(
        f"https://api.runpod.ai/v2/{endpoint_id}/run",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        json={
            "input": {
                "job_id": job_id,
                "blend_url": blend_url,
                "frame_start": frame_start,
                "frame_end": frame_end,
                "frame_step": frame_step,
                "render_overrides_b64": render_overrides_b64,
                "backend_url": PUBLIC_BACKEND_URL,
            }
        },
        timeout=30,
    )
    resp.raise_for_status()
    runpod_job_id: str = resp.json()["id"]
    log.info(f"Dispatched job {job_id} -> RunPod endpoint {endpoint_id} ({runpod_job_id})")
    return runpod_job_id


def cancel_job(runpod_job_id: str, machine_id: str = "") -> None:
    endpoint_id = (
        _endpoint_id_for_machine(machine_id) if machine_id else ENDPOINTS[0]["id"]
    )
    resp = httpx.post(
        f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{runpod_job_id}",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    log.info(f"Cancelled RunPod job {runpod_job_id} on endpoint {endpoint_id}")


def dispatch_and_save(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
    machine_id: str,
) -> str:
    """Dispatch to RunPod and immediately persist the RunPod job ID."""
    rp_job_id = dispatch_job(
        job_id=job_id,
        blend_url=blend_url,
        frame_start=frame_start,
        frame_end=frame_end,
        frame_step=frame_step,
        render_overrides_b64=render_overrides_b64,
        machine_id=machine_id,
    )
    execute("UPDATE jobs SET runpod_job_id = %s WHERE id = %s", (rp_job_id, job_id))
    return rp_job_id


def _runpod_status_hint(data: dict) -> str:
    values = [
        data.get("status"),
        data.get("delayReason"),
        data.get("delay_reason"),
        data.get("error"),
        data.get("message"),
        data.get("workerStatus"),
        data.get("worker_status"),
        data.get("executionStatus"),
        data.get("execution_status"),
    ]
    return " ".join(str(v) for v in values if v).upper()


def start_polling_thread(
    job_id: str,
    runpod_job_id: str,
    machine_id: str = "",
    blend_url: str = "",
    render_overrides_b64: str = "",
    group_id: str = "",
) -> None:
    """
    Start a background thread that polls RunPod /status until the job
    completes or fails, then delegates retry/failover to DispatchCoordinator.
    Every exit path calls notify_job_terminal so the autoscaler can scale down.
    """
    endpoint_id = (
        _endpoint_id_for_machine(machine_id) if machine_id else ENDPOINTS[0]["id"]
    )

    def _poll():
        from scheduling.dispatch_coordinator import coordinator

        started_at = time.monotonic()
        first_in_progress_at = None
        last_rendered_frames = None
        last_frame_change_at = time.monotonic()

        try:
            while True:
                time.sleep(JOB_STATUS_POLL_INTERVAL_SEC)
                try:
                    resp = httpx.get(
                        f"https://api.runpod.ai/v2/{endpoint_id}/status/{runpod_job_id}",
                        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    rp_status: str = str(data.get("status", "")).upper()
                    delay_reason = str(
                        data.get("delayReason") or data.get("delay_reason") or ""
                    ).upper()
                    status_hint = _runpod_status_hint(data)
                    throttled_signal = "THROTTL" in status_hint
                    queue_like_status = (
                        rp_status in {"IN_QUEUE", "QUEUED", "THROTTLED"}
                        or "THROTTL" in delay_reason
                    )

                    job = query_one(
                        """
                        SELECT status, attempt, max_retries, frame_start, frame_end, frame_step,
                               rendered_frames, input_filename, render_overrides_json,
                               chunk_index, chunk_size_frames, priority, last_heartbeat_at,
                               heartbeat_phase, runpod_job_id
                        FROM jobs WHERE id = %s
                        """,
                        (job_id,),
                    )
                    if not job:
                        log.warning(f"Poll: job {job_id} not found in DB, stopping")
                        return

                    current_runpod_job_id = job.get("runpod_job_id")
                    if current_runpod_job_id and current_runpod_job_id != runpod_job_id:
                        log.info(
                            f"Poll: stopping stale RunPod poll for job {job_id} "
                            f"({runpod_job_id} superseded by {current_runpod_job_id})"
                        )
                        return

                    local_status: str = job["status"]
                    elapsed = time.monotonic() - started_at

                    # Throttled: cancel immediately and route to failover
                    if (
                        RUNPOD_KILL_THROTTLED_IMMEDIATELY
                        and throttled_signal
                        and rp_status not in {"COMPLETED", "IN_PROGRESS", "FAILED", "CANCELLED", "TIMED_OUT"}
                    ):
                        throttle_error = (
                            f"RunPod throttled (status={rp_status}, "
                            f"delay_reason={delay_reason or 'n/a'}); cancelling for failover"
                        )
                        log.warning(f"Job {job_id}: {throttle_error}")
                        try:
                            cancel_job(runpod_job_id, machine_id)
                        except Exception as ce:
                            log.warning(f"Cancel failed during throttling: {ce}")
                        rp_status = "FAILED"
                        data["error"] = throttle_error

                    elif queue_like_status and elapsed > IN_QUEUE_TIMEOUT_SEC:
                        queue_error = (
                            f"Worker stuck in queue for {elapsed:.0f}s "
                            f"(status={rp_status}, delay_reason={delay_reason or 'n/a'})"
                        )
                        log.warning(f"Job {job_id}: {queue_error}")
                        try:
                            cancel_job(runpod_job_id, machine_id)
                        except Exception as ce:
                            log.warning(f"Cancel failed during IN_QUEUE timeout: {ce}")
                        rp_status = "FAILED"
                        data["error"] = queue_error

                    elif rp_status == "IN_PROGRESS":
                        if first_in_progress_at is None:
                            first_in_progress_at = time.monotonic()
                        cur_frames = job.get("rendered_frames") or 0
                        if cur_frames != last_rendered_frames:
                            last_rendered_frames = cur_frames
                            last_frame_change_at = time.monotonic()
                        if cur_frames == 0:
                            init_stall_error = ""
                            hb_age = _heartbeat_age_seconds(job.get("last_heartbeat_at"))
                            if hb_age is not None and hb_age > RUNPOD_INIT_STALL_SEC:
                                init_stall_error = (
                                    f"Worker IN_PROGRESS with 0 rendered frames and heartbeat stopped "
                                    f"for {hb_age:.0f}s (phase={job.get('heartbeat_phase') or 'unknown'})"
                                )
                            elif (
                                hb_age is None
                                and first_in_progress_at is not None
                                and (time.monotonic() - first_in_progress_at) > RUNPOD_INIT_STALL_SEC
                            ):
                                init_stall_error = (
                                    f"Worker IN_PROGRESS for {RUNPOD_INIT_STALL_SEC:.0f}s "
                                    f"but rendered_frames still 0"
                                )
                            if init_stall_error:
                                log.warning(f"Job {job_id}: {init_stall_error}")
                                try:
                                    cancel_job(runpod_job_id, machine_id)
                                except Exception as ce:
                                    log.warning(f"Cancel failed during init stall handling: {ce}")
                                coordinator.handle_failure(
                                    job_id=job_id,
                                    job=job,
                                    error=init_stall_error,
                                    blend_url=blend_url,
                                    render_overrides_b64=render_overrides_b64,
                                    failed_machine_id=machine_id,
                                    group_id=group_id,
                                )
                                return
                        elif time.monotonic() - last_frame_change_at > IN_PROGRESS_STALE_SEC:
                            timeout_err = (
                                f"RunPod job IN_PROGRESS but no new frames for "
                                f"{IN_PROGRESS_STALE_SEC/60:.0f} min — cancelling"
                            )
                            log.warning(f"Job {job_id}: {timeout_err}")
                            try:
                                cancel_job(runpod_job_id, machine_id)
                            except Exception as ce:
                                log.warning(f"Cancel failed during IN_PROGRESS timeout: {ce}")
                            rp_status = "FAILED"
                            data["error"] = timeout_err

                    # State transitions
                    if rp_status == "IN_PROGRESS" and local_status == "pending":
                        execute("UPDATE jobs SET status = 'running' WHERE id = %s", (job_id,))
                        log.info(f"Job {job_id} marked running (RunPod IN_PROGRESS)")

                    elif rp_status == "COMPLETED":
                        if local_status not in ("done", "failed", "cancelled"):
                            log.warning(
                                f"Job {job_id} RunPod COMPLETED but local={local_status}, forcing done"
                            )
                            execute(
                                "UPDATE jobs SET status = 'done', completed_at = %s WHERE id = %s",
                                (now_iso(), job_id),
                            )
                        return

                    elif rp_status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                        if local_status in ("done", "failed", "cancelled"):
                            return
                        error = str(data.get("error") or f"RunPod status: {rp_status}")
                        coordinator.handle_failure(
                            job_id=job_id,
                            job=job,
                            error=error,
                            blend_url=blend_url,
                            render_overrides_b64=render_overrides_b64,
                            failed_machine_id=machine_id,
                            group_id=group_id,
                        )
                        return

                except Exception as e:
                    log.error(f"RunPod poll error for job {job_id}: {e}")
        finally:
            # Every exit path notifies the autoscaler so it can scale down
            runpod_autoscaler.notify_job_terminal(job_id, endpoint_id)

    t = threading.Thread(target=_poll, daemon=True, name=f"runpod-poll-{job_id[:8]}")
    t.start()
