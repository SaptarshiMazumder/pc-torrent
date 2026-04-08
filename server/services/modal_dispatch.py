"""
Modal serverless dispatch layer (multi-endpoint).

Responsibilities:
- Register/maintain a virtual "modal_serverless" machine per endpoint
- Keep heartbeats alive so they stay visible in the available machines list
- Dispatch render jobs to the correct Modal web endpoint
- Monitor for stuck jobs and trigger failover when needed

Workers call back to the backend directly (same HTTP callbacks as RunPod
workers), so there is no need to actively poll Modal for status.  The
monitoring thread only checks the DB for stuck/stale jobs.

Env var format:
  MODAL_TOKEN_ID=ak-xxx
  MODAL_TOKEN_SECRET=as-xxx
  MODAL_ENDPOINTS=a10g:Modal A10G 24GB,l4:Modal L4 24GB
  MODAL_APP_NAME=pcrent-render
"""

import logging
import os
import threading
import time
from uuid import uuid4

import httpx

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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODAL_TOKEN_ID = os.getenv("MODAL_TOKEN_ID", "")
MODAL_TOKEN_SECRET = os.getenv("MODAL_TOKEN_SECRET", "")
MODAL_APP_NAME = os.getenv("MODAL_APP_NAME", "pcrent-render")

# Specs reported to the available machines list (shared across all endpoints)
MODAL_GPU_VRAM_GB = _env_float("MODAL_GPU_VRAM_GB", 24.0)
MODAL_CPU_CORES = _env_int("MODAL_CPU_CORES", 16)
MODAL_RAM_GB = _env_float("MODAL_RAM_GB", 64.0)

# Public URL your server is reachable at from inside Modal workers
PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000")

# Monitoring: how often to check the DB for stuck Modal jobs (seconds)
MONITOR_INTERVAL_SEC = 30

# Timeout: job stuck in 'pending' (cold start / queue). Mirrors RunPod logic.
IN_QUEUE_TIMEOUT_SEC = _env_float("IN_QUEUE_TIMEOUT_SEC", 120)

# Timeout: job IN_PROGRESS but rendered_frames hasn't changed.
IN_PROGRESS_STALE_SEC = _env_float("IN_PROGRESS_STALE_SEC", 90 * 60)

# Workers per endpoint for parallel expansion
try:
    MODAL_WORKERS_PER_ENDPOINT = max(1, int(os.getenv("MODAL_WORKERS_PER_ENDPOINT", "3")))
except ValueError:
    MODAL_WORKERS_PER_ENDPOINT = 3

# Heartbeat cadence for virtual machine rows (seconds)
_HEARTBEAT_INTERVAL = 10


# ---------------------------------------------------------------------------
# Endpoint parsing
# ---------------------------------------------------------------------------

def _parse_endpoints() -> list[dict]:
    """
    Parse Modal endpoints from env vars.

    Format:  MODAL_ENDPOINTS=gpu_type:Label,gpu_type:Label
    Example: MODAL_ENDPOINTS=a10g:Modal A10G 24GB,l4:Modal L4 24GB

    Each gpu_type maps to a deployed Modal web endpoint function named
    render_{gpu_type}  (e.g. render_a10g, render_l4).
    """
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
            endpoints.append({"id": gpu_type.strip(), "label": label.strip()})
        else:
            endpoints.append({"id": part, "label": f"Modal {part.upper()}"})
    return endpoints


ENDPOINTS = _parse_endpoints()

# machine_id -> endpoint gpu_type (populated during registration)
_machine_endpoint_map: dict[str, str] = {}


def _endpoint_url(gpu_type: str) -> str:
    """Build the Modal web endpoint URL for a given GPU type."""
    # Modal web endpoint URLs follow the pattern:
    #   https://{workspace}--{app_name}-render-{gpu_type}.modal.run
    # Since the workspace prefix is part of the URL and we can't know it,
    # the user can override with MODAL_ENDPOINT_URL_PREFIX.
    prefix = os.getenv("MODAL_ENDPOINT_URL_PREFIX", "").strip().rstrip("/")
    if prefix:
        if "{gpu_type}" in prefix:
            return prefix.format(gpu_type=gpu_type)

        # Full function URL already provided.
        if f"-render-{gpu_type}.modal.run" in prefix:
            return prefix

        # Prefix may be a Modal host (e.g. https://workspace--app.modal.run).
        if prefix.endswith(".modal.run"):
            return prefix.replace(".modal.run", f"-render-{gpu_type}.modal.run")

        # Common shorthand in env files: https://workspace--app
        if "--" in prefix:
            return f"{prefix}-render-{gpu_type}.modal.run"

        # Backward-compatible fallback for non-Modal custom prefixes.
        return f"{prefix}/render-{gpu_type}"
    # If no prefix, try constructing from MODAL_WORKSPACE
    workspace = os.getenv("MODAL_WORKSPACE", "").strip()
    if workspace:
        return f"https://{workspace}--{MODAL_APP_NAME}-render-{gpu_type}.modal.run"
    raise ValueError(
        "Cannot construct Modal endpoint URL: set MODAL_ENDPOINT_URL_PREFIX or "
        "MODAL_WORKSPACE env var"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return bool(MODAL_TOKEN_ID and MODAL_TOKEN_SECRET and ENDPOINTS)


def _deactivate_stale_virtual_machines(db_execute, active_machine_keys: list[str]) -> None:
    """Mark any removed Modal endpoints as idle."""
    if not active_machine_keys:
        db_execute(
            """
            UPDATE machines
            SET status = 'idle'
            WHERE machine_type = 'modal_serverless'
            """
        )
        return

    placeholders = ", ".join(["%s"] * len(active_machine_keys))
    db_execute(
        f"""
        UPDATE machines
        SET status = 'idle'
        WHERE machine_type = 'modal_serverless'
          AND machine_key NOT IN ({placeholders})
        """,
        tuple(active_machine_keys),
    )


def register_virtual_machines(db_execute, db_query_one, now_iso) -> list[str]:
    """
    Upsert one virtual machine row per Modal endpoint.
    Returns list of machine_ids, or empty list if Modal is not configured.
    """
    if not is_enabled():
        _machine_endpoint_map.clear()
        _deactivate_stale_virtual_machines(db_execute, [])
        log.info("Modal not configured (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET / MODAL_ENDPOINTS unset) - skipping")
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

        existing = db_query_one(
            "SELECT id FROM machines WHERE machine_key = %s", (machine_key,)
        )

        if existing:
            machine_id = existing["id"]
            db_execute(
                """
                UPDATE machines
                SET gpu_model = %s, gpu_vram_gb = %s, cpu_cores = %s, ram_gb = %s,
                    os_version = %s, machine_type = %s,
                    status = 'available', last_seen_at = %s
                WHERE id = %s
                """,
                (
                    label, MODAL_GPU_VRAM_GB, MODAL_CPU_CORES, MODAL_RAM_GB,
                    "Linux", "modal_serverless", now, machine_id,
                ),
            )
            log.info(f"Modal endpoint {gpu_type} updated: {machine_id} ({label})")
        else:
            machine_id = str(uuid4())
            db_execute(
                """
                INSERT INTO machines (
                    id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb,
                    os_version, machine_type, status, registered_at, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'available', %s, %s)
                """,
                (
                    machine_id, machine_key,
                    label, MODAL_GPU_VRAM_GB, MODAL_CPU_CORES, MODAL_RAM_GB,
                    "Linux", "modal_serverless", now, now,
                ),
            )
            log.info(f"Modal endpoint {gpu_type} registered: {machine_id} ({label})")

        _machine_endpoint_map[machine_id] = gpu_type
        machine_ids.append(machine_id)

    _deactivate_stale_virtual_machines(db_execute, active_machine_keys)
    return machine_ids


def start_heartbeat_thread(db_execute, now_iso):
    """
    Background thread: refreshes last_seen_at for all Modal virtual
    machines every _HEARTBEAT_INTERVAL seconds.
    """
    if not is_enabled():
        return

    machine_keys = [f"modal-serverless-{ep['id']}" for ep in ENDPOINTS]

    def _loop():
        while True:
            try:
                for mk in machine_keys:
                    db_execute(
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
    """Resolve which GPU type / endpoint a machine_id maps to."""
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
    """
    POST a job to the correct Modal web endpoint.
    Returns a job identifier (the Modal call ID from headers, or a generated one).
    Raises on failure.
    """
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

    resp = httpx.post(
        url,
        headers={
            "Modal-Key": MODAL_TOKEN_ID,
            "Modal-Secret": MODAL_TOKEN_SECRET,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()

    # Modal web endpoints return the function result synchronously for short
    # calls.  For long-running renders the connection stays open.  We use a
    # generated ID since Modal doesn't return a call ID in the same way RunPod
    # does.  The monitoring thread tracks the job via DB state instead.
    modal_job_id = f"modal-{job_id[:12]}"
    log.info(f"Dispatched job {job_id} -> Modal endpoint {gpu_type} ({modal_job_id})")
    return modal_job_id


def cancel_job(provider_job_id: str, machine_id: str = ""):
    """
    Cancel a running Modal job.

    Modal web endpoints don't expose a cancel-by-call-id REST endpoint.
    The worker will detect that the job status changed to 'cancelled' in the
    DB and should stop on its own.  This is a no-op placeholder; the actual
    cancellation is handled by the backend marking the job as cancelled.
    """
    log.info(f"Modal cancel requested for {provider_job_id} (handled via DB status)")


def dispatch_and_save(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
    machine_id: str,
    db_execute,
) -> str:
    """Dispatch to Modal and persist the job ID on the job row (reuses runpod_job_id column)."""
    modal_job_id = dispatch_job(
        job_id=job_id,
        blend_url=blend_url,
        frame_start=frame_start,
        frame_end=frame_end,
        frame_step=frame_step,
        render_overrides_b64=render_overrides_b64,
        machine_id=machine_id,
    )
    db_execute(
        "UPDATE jobs SET runpod_job_id = %s WHERE id = %s",
        (modal_job_id, job_id),
    )
    return modal_job_id


def _find_failover_machine(db_query_one, db_query_all, failed_machine_id: str):
    """
    Find the best available machine to take over a failed job.
    Prefers: other serverless endpoints > available linux/windows PCs.
    Returns (machine_row, machine_type) or (None, None).
    """
    rows = db_query_all(
        """
        SELECT * FROM machines
        WHERE status = 'available' AND id != %s
        ORDER BY gpu_vram_gb DESC
        """,
        (failed_machine_id,),
    )
    if not rows:
        return None, None
    # Prefer any serverless (instant spin-up), then others
    serverless = [
        r for r in rows
        if r.get("machine_type") in ("modal_serverless", "runpod_serverless")
    ]
    if serverless:
        return serverless[0], serverless[0]["machine_type"]
    return rows[0], rows[0].get("machine_type", "windows")


def start_monitoring_thread(
    job_id: str,
    provider_job_id: str,
    db_execute,
    db_query_one,
    now_iso,
    machine_id: str = "",
    blend_url: str = "",
    render_overrides_b64: str = "",
    db_query_all=None,
    group_id: str = "",
):
    """
    Background thread: monitors a Modal job by checking the DB periodically.

    Modal workers call back to the backend directly (PUT /jobs/{id}/status,
    PUT /jobs/{id}/progress), so we don't need to poll Modal's API.  This
    thread only detects stuck jobs (no progress for too long, stuck in
    pending for too long) and triggers failover.
    """

    def _monitor():
        started_at = time.monotonic()
        last_rendered_frames = None
        last_frame_change_at = time.monotonic()

        while True:
            time.sleep(MONITOR_INTERVAL_SEC)
            try:
                job = db_query_one(
                    """SELECT status, attempt, max_retries, frame_start, frame_end,
                              frame_step, rendered_frames, input_filename,
                              render_overrides_json, chunk_index, chunk_size_frames,
                              priority
                       FROM jobs WHERE id = %s""",
                    (job_id,),
                )
                if not job:
                    log.warning(f"Monitor: job {job_id} not found in DB, stopping")
                    break

                local_status = job["status"]

                # Job already finished
                if local_status in ("done", "failed", "cancelled"):
                    log.info(f"Monitor: job {job_id} is {local_status}, stopping")
                    break

                elapsed = time.monotonic() - started_at

                # Stuck in pending (cold start / queue timeout)
                if local_status == "pending" and elapsed > IN_QUEUE_TIMEOUT_SEC:
                    queue_error = (
                        f"Modal job stuck in pending for {elapsed:.0f}s - "
                        f"cancelling and routing to failover"
                    )
                    log.warning(f"Job {job_id}: {queue_error}")
                    _handle_failure(
                        job_id=job_id, job=job, error=queue_error,
                        blend_url=blend_url,
                        render_overrides_b64=render_overrides_b64,
                        machine_id=machine_id, group_id=group_id,
                        db_execute=db_execute, db_query_one=db_query_one,
                        db_query_all=db_query_all, now_iso=now_iso,
                    )
                    break

                # In progress but no new frames for too long
                if local_status == "running":
                    cur_frames = job.get("rendered_frames") or 0
                    if cur_frames != last_rendered_frames:
                        last_rendered_frames = cur_frames
                        last_frame_change_at = time.monotonic()
                    elif time.monotonic() - last_frame_change_at > IN_PROGRESS_STALE_SEC:
                        stale_error = (
                            f"Modal job running but no new frames for "
                            f"{IN_PROGRESS_STALE_SEC/60:.0f} min - cancelling"
                        )
                        log.warning(f"Job {job_id}: {stale_error}")
                        _handle_failure(
                            job_id=job_id, job=job, error=stale_error,
                            blend_url=blend_url,
                            render_overrides_b64=render_overrides_b64,
                            machine_id=machine_id, group_id=group_id,
                            db_execute=db_execute, db_query_one=db_query_one,
                            db_query_all=db_query_all, now_iso=now_iso,
                        )
                        break

            except Exception as e:
                log.error(f"Modal monitor error for job {job_id}: {e}")
                # keep retrying - transient issues shouldn't kill the loop

    t = threading.Thread(target=_monitor, daemon=True, name=f"modal-mon-{job_id[:8]}")
    t.start()


def _handle_failure(
    job_id, job, error, blend_url, render_overrides_b64,
    machine_id, group_id, db_execute, db_query_one, db_query_all, now_iso,
):
    """Handle a failed/stuck Modal job: retry on same endpoint or failover."""
    import uuid

    rendered = max(0, job.get("rendered_frames") or 0)
    step = job.get("frame_step") or 1
    remaining_start = job["frame_start"] + rendered * step
    remaining_end = job["frame_end"]

    # --- Attempt 1: retry on same endpoint ---
    attempt = job.get("attempt") or 0
    max_retries = job.get("max_retries") or 0
    if attempt < max_retries and blend_url and remaining_start <= remaining_end:
        next_attempt = attempt + 1
        db_execute(
            """
            UPDATE jobs
            SET status = 'pending', attempt = %s,
                rendered_frames = 0, error = %s,
                frame_start = %s
            WHERE id = %s
            """,
            (next_attempt, f"Retry {next_attempt}/{max_retries} (was: {error})", remaining_start, job_id),
        )
        log.warning(
            f"Job {job_id} failed, retrying on same Modal endpoint "
            f"({next_attempt}/{max_retries}): {error}"
        )
        try:
            new_job_id_str = dispatch_and_save(
                job_id=job_id,
                blend_url=blend_url,
                frame_start=remaining_start,
                frame_end=remaining_end,
                frame_step=step,
                render_overrides_b64=render_overrides_b64,
                machine_id=machine_id,
                db_execute=db_execute,
            )
            start_monitoring_thread(
                job_id=job_id,
                provider_job_id=new_job_id_str,
                db_execute=db_execute,
                db_query_one=db_query_one,
                now_iso=now_iso,
                machine_id=machine_id,
                blend_url=blend_url,
                render_overrides_b64=render_overrides_b64,
                db_query_all=db_query_all,
                group_id=group_id,
            )
            return
        except Exception as dispatch_err:
            log.error(f"Same-endpoint retry failed for {job_id}: {dispatch_err}")
            error = str(dispatch_err)

    # --- Attempt 2: failover to any available machine ---
    if blend_url and remaining_start <= remaining_end and db_query_all:
        _handle_failover(
            job_id=job_id, job=job, error=error,
            remaining_start=remaining_start, remaining_end=remaining_end,
            step=step, blend_url=blend_url,
            render_overrides_b64=render_overrides_b64,
            failed_machine_id=machine_id, group_id=group_id,
            db_execute=db_execute, db_query_one=db_query_one,
            db_query_all=db_query_all, now_iso=now_iso,
        )
    else:
        db_execute(
            """
            UPDATE jobs
            SET status = 'failed', completed_at = %s, error = %s
            WHERE id = %s
            """,
            (now_iso(), error, job_id),
        )
        log.error(f"Job {job_id} failed, no failover possible: {error}")


def _handle_failover(
    job_id, job, error, remaining_start, remaining_end, step,
    blend_url, render_overrides_b64, failed_machine_id, group_id,
    db_execute, db_query_one, db_query_all, now_iso,
):
    """Mark original job failed, create a new job on the best available machine."""
    import uuid

    db_execute(
        """
        UPDATE jobs
        SET status = 'failed', completed_at = %s, error = %s
        WHERE id = %s
        """,
        (now_iso(), f"Failed, migrating remaining frames ({error})", job_id),
    )

    failover_machine, failover_type = _find_failover_machine(
        db_query_one, db_query_all, failed_machine_id,
    )
    if not failover_machine:
        log.error(f"Job {job_id}: no available machines for failover")
        return

    new_total = ((remaining_end - remaining_start) // step) + 1
    new_job_id = str(uuid.uuid4())
    db_execute(
        """
        INSERT INTO jobs (id, machine_id, group_id, input_filename, status,
                          total_frames, rendered_frames, output_files,
                          frame_start, frame_end, frame_step,
                          render_overrides_json, attempt, max_retries, priority,
                          chunk_index, chunk_size_frames, submitted_at)
        VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, 0, %s, %s, %s, %s, %s)
        """,
        (
            new_job_id, failover_machine["id"], group_id,
            job.get("input_filename"), new_total,
            remaining_start, remaining_end, step,
            job.get("render_overrides_json") or "{}",
            job.get("max_retries") or 0,
            job.get("priority") or 0,
            job.get("chunk_index"),
            job.get("chunk_size_frames"),
            now_iso(),
        ),
    )
    log.info(
        f"Job {job_id} failed over -> new job {new_job_id} on "
        f"{failover_machine.get('gpu_model', '?')} ({failover_type})"
    )

    # Dispatch the new job if it's serverless
    if failover_type == "modal_serverless":
        try:
            modal_job_id = dispatch_and_save(
                job_id=new_job_id,
                blend_url=blend_url,
                frame_start=remaining_start,
                frame_end=remaining_end,
                frame_step=step,
                render_overrides_b64=render_overrides_b64,
                machine_id=failover_machine["id"],
                db_execute=db_execute,
            )
            start_monitoring_thread(
                job_id=new_job_id,
                provider_job_id=modal_job_id,
                db_execute=db_execute,
                db_query_one=db_query_one,
                now_iso=now_iso,
                machine_id=failover_machine["id"],
                blend_url=blend_url,
                render_overrides_b64=render_overrides_b64,
                db_query_all=db_query_all,
                group_id=group_id,
            )
        except Exception as exc:
            log.error(f"Failover dispatch to Modal failed for new job {new_job_id}: {exc}")
            db_execute(
                "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
                (now_iso(), f"Failover dispatch failed: {exc}", new_job_id),
            )
    elif failover_type == "runpod_serverless":
        # Cross-provider failover: import RunPod dispatch for RunPod targets
        try:
            from services import runpod_dispatch
            if runpod_dispatch.is_enabled():
                rp_job_id = runpod_dispatch.dispatch_and_save(
                    job_id=new_job_id,
                    blend_url=blend_url,
                    frame_start=remaining_start,
                    frame_end=remaining_end,
                    frame_step=step,
                    render_overrides_b64=render_overrides_b64,
                    machine_id=failover_machine["id"],
                    db_execute=db_execute,
                )
                runpod_dispatch.start_polling_thread(
                    job_id=new_job_id,
                    runpod_job_id=rp_job_id,
                    db_execute=db_execute,
                    db_query_one=db_query_one,
                    now_iso=now_iso,
                    machine_id=failover_machine["id"],
                    blend_url=blend_url,
                    render_overrides_b64=render_overrides_b64,
                    db_query_all=db_query_all,
                    group_id=group_id,
                )
        except Exception as exc:
            log.error(f"Failover dispatch to RunPod failed for new job {new_job_id}: {exc}")
            db_execute(
                "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
                (now_iso(), f"Cross-provider failover dispatch failed: {exc}", new_job_id),
            )
