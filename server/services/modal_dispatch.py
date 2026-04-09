"""
Modal serverless dispatch layer (multi-endpoint).

Responsibilities:
- Register/maintain a virtual "modal_serverless" machine per endpoint
- Keep heartbeats alive so they stay visible in the available machines list
- Dispatch render jobs to the correct Modal web endpoint
- Monitor for stuck jobs and delegate failover to DispatchCoordinator

Workers call back to the backend directly (same HTTP callbacks as RunPod
workers), so there is no need to actively poll Modal for status.  The
monitoring thread only checks the DB for stuck/stale jobs.

Env var format:
  MODAL_TOKEN_ID=ak-xxx
  MODAL_TOKEN_SECRET=as-xxx
  MODAL_ENDPOINTS=a10g:Modal A10G 24GB,l4:Modal L4 24GB
  MODAL_APP_NAME=pcrent-render
"""

from __future__ import annotations

import logging
import os
import threading
import time
from uuid import uuid4

import httpx

from domain.value_objects import now_iso
from infrastructure.db import execute, query_one

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


def _env_csv_set(name: str, default: str = "") -> set[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODAL_TOKEN_ID = os.getenv("MODAL_TOKEN_ID", "")
MODAL_TOKEN_SECRET = os.getenv("MODAL_TOKEN_SECRET", "")
MODAL_APP_NAME = os.getenv("MODAL_APP_NAME", "pcrent-render")
MODAL_DISABLED_GPU_TYPES = _env_csv_set("MODAL_DISABLED_GPU_TYPES", "a100")

MODAL_GPU_VRAM_GB = _env_float("MODAL_GPU_VRAM_GB", 24.0)
MODAL_CPU_CORES = _env_int("MODAL_CPU_CORES", 16)
MODAL_RAM_GB = _env_float("MODAL_RAM_GB", 64.0)

PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000")

MONITOR_INTERVAL_SEC = 30

_raw_modal_dispatch_timeout = _env_float("MODAL_DISPATCH_TIMEOUT_SEC", 6 * 60 * 60)
MODAL_DISPATCH_TIMEOUT_SEC: float | None = (
    None if _raw_modal_dispatch_timeout <= 0 else _raw_modal_dispatch_timeout
)

IN_QUEUE_TIMEOUT_SEC = _env_float("IN_QUEUE_TIMEOUT_SEC", 120)
IN_PROGRESS_STALE_SEC = _env_float("IN_PROGRESS_STALE_SEC", 90 * 60)

try:
    MODAL_WORKERS_PER_ENDPOINT = max(1, int(os.getenv("MODAL_WORKERS_PER_ENDPOINT", "3")))
except ValueError:
    MODAL_WORKERS_PER_ENDPOINT = 3

_HEARTBEAT_INTERVAL = 10


# ---------------------------------------------------------------------------
# Endpoint parsing
# ---------------------------------------------------------------------------

def _parse_endpoints() -> list[dict]:
    raw = os.getenv("MODAL_ENDPOINTS", "").strip()
    if not raw:
        return []

    endpoints = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            gpu_type, label = part.split(":", 1)
            gpu_type = gpu_type.strip()
            if gpu_type.lower() in MODAL_DISABLED_GPU_TYPES:
                log.warning(f"Skipping disabled Modal endpoint gpu_type={gpu_type}")
                continue
            endpoints.append({"id": gpu_type, "label": label.strip()})
        else:
            gpu_type = part.strip()
            if gpu_type.lower() in MODAL_DISABLED_GPU_TYPES:
                log.warning(f"Skipping disabled Modal endpoint gpu_type={gpu_type}")
                continue
            endpoints.append({"id": gpu_type, "label": f"Modal {gpu_type.upper()}"})
    return endpoints


ENDPOINTS = _parse_endpoints()

# machine_id -> endpoint gpu_type
_machine_endpoint_map: dict[str, str] = {}


def _endpoint_url(gpu_type: str) -> str:
    prefix = os.getenv("MODAL_ENDPOINT_URL_PREFIX", "").strip().rstrip("/")
    if prefix:
        if "{gpu_type}" in prefix:
            return prefix.format(gpu_type=gpu_type)
        if f"-render-{gpu_type}.modal.run" in prefix:
            return prefix
        if prefix.endswith(".modal.run"):
            return prefix.replace(".modal.run", f"-render-{gpu_type}.modal.run")
        if "--" in prefix:
            return f"{prefix}-render-{gpu_type}.modal.run"
        return f"{prefix}/render-{gpu_type}"
    workspace = os.getenv("MODAL_WORKSPACE", "").strip()
    if workspace:
        return f"https://{workspace}--{MODAL_APP_NAME}-render-{gpu_type}.modal.run"
    raise ValueError(
        "Cannot construct Modal endpoint URL: set MODAL_ENDPOINT_URL_PREFIX or MODAL_WORKSPACE"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return bool(MODAL_TOKEN_ID and MODAL_TOKEN_SECRET and ENDPOINTS)


def _deactivate_stale_virtual_machines(active_machine_keys: list[str]) -> None:
    if not active_machine_keys:
        execute("UPDATE machines SET status = 'idle' WHERE machine_type = 'modal_serverless'")
        return
    placeholders = ", ".join(["%s"] * len(active_machine_keys))
    execute(
        f"""
        UPDATE machines
        SET status = 'idle'
        WHERE machine_type = 'modal_serverless'
          AND machine_key NOT IN ({placeholders})
        """,
        tuple(active_machine_keys),
    )


def register_virtual_machines() -> list[str]:
    """Upsert one virtual machine row per Modal endpoint."""
    if not (MODAL_TOKEN_ID and MODAL_TOKEN_SECRET):
        _machine_endpoint_map.clear()
        _deactivate_stale_virtual_machines([])
        log.info("Modal not configured — skipping registration")
        return []
    if not ENDPOINTS:
        _machine_endpoint_map.clear()
        _deactivate_stale_virtual_machines([])
        log.info("Modal configured but no active endpoints after filtering")
        return []

    _machine_endpoint_map.clear()
    machine_ids = []
    active_machine_keys = []
    now = now_iso()

    for ep in ENDPOINTS:
        gpu_type = ep["id"]
        label = ep["label"]
        machine_key = f"modal-serverless-{gpu_type}"
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
                (label, MODAL_GPU_VRAM_GB, MODAL_CPU_CORES, MODAL_RAM_GB,
                 "Linux", "modal_serverless", now, machine_id),
            )
            log.info(f"Modal endpoint {gpu_type} updated: {machine_id} ({label})")
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
                 label, MODAL_GPU_VRAM_GB, MODAL_CPU_CORES, MODAL_RAM_GB,
                 "Linux", "modal_serverless", now, now),
            )
            log.info(f"Modal endpoint {gpu_type} registered: {machine_id} ({label})")

        _machine_endpoint_map[machine_id] = gpu_type
        machine_ids.append(machine_id)

    _deactivate_stale_virtual_machines(active_machine_keys)
    return machine_ids


def start_heartbeat_thread() -> None:
    if not is_enabled():
        return

    machine_keys = [f"modal-serverless-{ep['id']}" for ep in ENDPOINTS]

    def _loop():
        while True:
            try:
                for mk in machine_keys:
                    execute(
                        """
                        UPDATE machines
                        SET last_seen_at = %s
                        WHERE machine_key = %s AND machine_type = 'modal_serverless'
                        """,
                        (now_iso(), mk),
                    )
            except Exception as e:
                log.warning(f"Modal heartbeat error: {e}")
            time.sleep(_HEARTBEAT_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="modal-heartbeat")
    t.start()
    log.info(f"Modal heartbeat thread started ({len(ENDPOINTS)} endpoint(s))")


def _gpu_type_for_machine(machine_id: str) -> str:
    gpu_type = _machine_endpoint_map.get(machine_id)
    if gpu_type:
        return gpu_type
    if len(ENDPOINTS) == 1:
        return ENDPOINTS[0]["id"]
    raise ValueError(f"No Modal endpoint mapped for machine_id={machine_id}")


def dispatch_job(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
    machine_id: str = "",
) -> str:
    """POST a job to the correct Modal web endpoint. Returns a job identifier."""
    gpu_type = _gpu_type_for_machine(machine_id)
    url = _endpoint_url(gpu_type)

    payload = {
        "input": {
            "job_id": job_id,
            "blend_url": blend_url,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "render_overrides_b64": render_overrides_b64,
            "backend_url": PUBLIC_BACKEND_URL,
        }
    }

    log.info(f"Dispatching job {job_id} to Modal endpoint {gpu_type} url={url}")
    try:
        resp = httpx.post(
            url,
            headers={
                "Modal-Key": MODAL_TOKEN_ID,
                "Modal-Secret": MODAL_TOKEN_SECRET,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=MODAL_DISPATCH_TIMEOUT_SEC,
        )
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Modal dispatch timeout for job {job_id} endpoint={gpu_type} "
            f"timeout={MODAL_DISPATCH_TIMEOUT_SEC}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = (exc.response.text or "")[:300]
        except Exception:
            body = ""
        raise RuntimeError(
            f"Modal dispatch HTTP {exc.response.status_code} for job {job_id} "
            f"endpoint={gpu_type} body={body}"
        ) from exc

    modal_job_id = f"modal-{job_id[:12]}"
    log.info(f"Dispatched job {job_id} -> Modal endpoint {gpu_type} ({modal_job_id})")
    return modal_job_id


def cancel_job(provider_job_id: str, machine_id: str = "") -> None:
    """No-op: Modal cancellation is handled via DB status change."""
    log.info(f"Modal cancel requested for {provider_job_id} (handled via DB status)")


def dispatch_and_save(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
    machine_id: str,
) -> str:
    """Dispatch to Modal and persist the job ID on the job row."""
    modal_job_id = dispatch_job(
        job_id=job_id,
        blend_url=blend_url,
        frame_start=frame_start,
        frame_end=frame_end,
        frame_step=frame_step,
        render_overrides_b64=render_overrides_b64,
        machine_id=machine_id,
    )
    execute("UPDATE jobs SET runpod_job_id = %s WHERE id = %s", (modal_job_id, job_id))
    return modal_job_id


def start_monitoring_thread(
    job_id: str,
    provider_job_id: str,
    machine_id: str = "",
    blend_url: str = "",
    render_overrides_b64: str = "",
    group_id: str = "",
) -> None:
    """
    Start a background thread that monitors a Modal job by polling the DB.

    Modal workers call back directly, so this thread only detects stuck jobs
    (pending too long, or no frame progress for too long) and delegates
    failure handling to DispatchCoordinator.
    """

    def _monitor():
        from scheduling.dispatch_coordinator import coordinator

        started_at = time.monotonic()
        last_rendered_frames = None
        last_frame_change_at = time.monotonic()

        while True:
            time.sleep(MONITOR_INTERVAL_SEC)
            try:
                job = query_one(
                    """
                    SELECT status, attempt, max_retries, frame_start, frame_end,
                           frame_step, rendered_frames, input_filename,
                           render_overrides_json, chunk_index, chunk_size_frames, priority
                    FROM jobs WHERE id = %s
                    """,
                    (job_id,),
                )
                if not job:
                    log.warning(f"Monitor: job {job_id} not found in DB, stopping")
                    break

                local_status = job["status"]

                if local_status in ("done", "failed", "cancelled"):
                    log.info(f"Monitor: job {job_id} is {local_status}, stopping")
                    break

                elapsed = time.monotonic() - started_at

                if local_status == "pending" and elapsed > IN_QUEUE_TIMEOUT_SEC:
                    queue_error = (
                        f"Modal job stuck in pending for {elapsed:.0f}s — routing to failover"
                    )
                    log.warning(f"Job {job_id}: {queue_error}")
                    coordinator.handle_failure(
                        job_id=job_id,
                        job=job,
                        error=queue_error,
                        blend_url=blend_url,
                        render_overrides_b64=render_overrides_b64,
                        failed_machine_id=machine_id,
                        group_id=group_id,
                    )
                    break

                if local_status == "running":
                    cur_frames = job.get("rendered_frames") or 0
                    if cur_frames != last_rendered_frames:
                        last_rendered_frames = cur_frames
                        last_frame_change_at = time.monotonic()
                    elif time.monotonic() - last_frame_change_at > IN_PROGRESS_STALE_SEC:
                        stale_error = (
                            f"Modal job running but no new frames for "
                            f"{IN_PROGRESS_STALE_SEC/60:.0f} min — cancelling"
                        )
                        log.warning(f"Job {job_id}: {stale_error}")
                        coordinator.handle_failure(
                            job_id=job_id,
                            job=job,
                            error=stale_error,
                            blend_url=blend_url,
                            render_overrides_b64=render_overrides_b64,
                            failed_machine_id=machine_id,
                            group_id=group_id,
                        )
                        break

            except Exception as e:
                log.error(f"Modal monitor error for job {job_id}: {e}")

    t = threading.Thread(target=_monitor, daemon=True, name=f"modal-mon-{job_id[:8]}")
    t.start()
