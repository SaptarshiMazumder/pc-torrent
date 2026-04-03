"""
RunPod serverless dispatch layer.

Responsibilities:
- Register/maintain a virtual "runpod_serverless" machine in the DB
- Keep its heartbeat alive so it stays visible in the marketplace
- Dispatch render jobs to the RunPod endpoint
- Poll RunPod /status/{id} until COMPLETED or FAILED and reconcile DB state
"""

import logging
import os
import threading
import time
from uuid import uuid4

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (all overridable via env vars)
# ---------------------------------------------------------------------------
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")

# Stable key — re-registering with same key updates instead of duplicating
RUNPOD_MACHINE_KEY = os.getenv("RUNPOD_MACHINE_KEY", "runpod-serverless-v1")

# Specs reported to the marketplace (tune to match your serverless GPU tier)
RUNPOD_GPU_MODEL = os.getenv("RUNPOD_GPU_MODEL", "RunPod Serverless (RTX 4090)")
RUNPOD_GPU_VRAM_GB = float(os.getenv("RUNPOD_GPU_VRAM_GB", "24"))
RUNPOD_CPU_CORES = int(os.getenv("RUNPOD_CPU_CORES", "16"))
RUNPOD_RAM_GB = float(os.getenv("RUNPOD_RAM_GB", "64"))

# Public URL your server is reachable at from inside RunPod workers
PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000")

# How often to poll RunPod /status (seconds)
JOB_STATUS_POLL_INTERVAL_SEC = float(os.getenv("JOB_STATUS_POLL_INTERVAL_SEC", "5"))

# Heartbeat cadence for the virtual machine row (seconds)
_HEARTBEAT_INTERVAL = 10


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return bool(RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID)


def register_virtual_machine(db_execute, db_query_one, now_iso) -> str | None:
    """
    Upsert the RunPod serverless virtual machine in the machines table.
    Returns the machine_id, or None if RunPod is not configured.
    """
    if not is_enabled():
        log.info("RunPod not configured (RUNPOD_API_KEY / RUNPOD_ENDPOINT_ID unset) — skipping")
        return None

    now = now_iso()
    existing = db_query_one(
        "SELECT id FROM machines WHERE machine_key = %s", (RUNPOD_MACHINE_KEY,)
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
                RUNPOD_GPU_MODEL, RUNPOD_GPU_VRAM_GB, RUNPOD_CPU_CORES, RUNPOD_RAM_GB,
                "Linux", "runpod_serverless", now, machine_id,
            ),
        )
        log.info(f"RunPod virtual machine updated: {machine_id}")
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
                machine_id, RUNPOD_MACHINE_KEY,
                RUNPOD_GPU_MODEL, RUNPOD_GPU_VRAM_GB, RUNPOD_CPU_CORES, RUNPOD_RAM_GB,
                "Linux", "runpod_serverless", now, now,
            ),
        )
        log.info(f"RunPod virtual machine registered: {machine_id}")

    return machine_id


def start_heartbeat_thread(db_execute, now_iso):
    """
    Background thread: refreshes last_seen_at every 10 s so the virtual
    machine stays visible in GET /machines (which filters by recency).
    """
    if not is_enabled():
        return

    def _loop():
        while True:
            try:
                db_execute(
                    """
                    UPDATE machines
                    SET last_seen_at = %s
                    WHERE machine_key = %s AND machine_type = 'runpod_serverless'
                    """,
                    (now_iso(), RUNPOD_MACHINE_KEY),
                )
            except Exception as e:
                log.warning(f"RunPod heartbeat error: {e}")
            time.sleep(_HEARTBEAT_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="runpod-heartbeat")
    t.start()
    log.info("RunPod heartbeat thread started")


def dispatch_job(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
) -> str:
    """
    POST a job to the RunPod serverless endpoint.
    Returns the RunPod job ID.
    Raises on failure.
    """
    resp = httpx.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
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
    log.info(f"Dispatched job {job_id} → RunPod {runpod_job_id}")
    return runpod_job_id


def start_polling_thread(job_id: str, runpod_job_id: str, db_execute, db_query_one, now_iso):
    """
    Background thread: polls RunPod /status/{runpod_job_id} every
    JOB_STATUS_POLL_INTERVAL_SEC seconds.

    State machine:
      IN_QUEUE     → do nothing (job is pending)
      IN_PROGRESS  → flip job to 'running' if it's still 'pending'
      COMPLETED    → safety-net: if the handler already called /status done,
                     this is a no-op; otherwise force done
      FAILED /
      CANCELLED    → mark job failed if not already terminal
    """

    def _poll():
        while True:
            time.sleep(JOB_STATUS_POLL_INTERVAL_SEC)
            try:
                resp = httpx.get(
                    f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{runpod_job_id}",
                    headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                rp_status: str = data.get("status", "")

                job = db_query_one(
                    "SELECT status FROM jobs WHERE id = %s", (job_id,)
                )
                if not job:
                    log.warning(f"Poll: job {job_id} not found in DB, stopping")
                    break

                local_status: str = job["status"]

                if rp_status == "IN_PROGRESS" and local_status == "pending":
                    db_execute(
                        "UPDATE jobs SET status = 'running' WHERE id = %s", (job_id,)
                    )
                    log.info(f"Job {job_id} marked running (RunPod IN_PROGRESS)")

                elif rp_status == "COMPLETED":
                    if local_status not in ("done", "failed"):
                        log.warning(
                            f"Job {job_id} RunPod COMPLETED but local={local_status}, forcing done"
                        )
                        db_execute(
                            "UPDATE jobs SET status = 'done', completed_at = %s WHERE id = %s",
                            (now_iso(), job_id),
                        )
                    break

                elif rp_status in ("FAILED", "CANCELLED"):
                    if local_status not in ("done", "failed"):
                        error = str(data.get("error") or f"RunPod status: {rp_status}")
                        db_execute(
                            """
                            UPDATE jobs
                            SET status = 'failed', completed_at = %s, error = %s
                            WHERE id = %s
                            """,
                            (now_iso(), error, job_id),
                        )
                        log.error(f"Job {job_id} failed on RunPod: {error}")
                    break

                # IN_QUEUE or unknown → keep polling

            except Exception as e:
                log.error(f"RunPod poll error for job {job_id}: {e}")
                # keep retrying — transient network issues shouldn't kill the loop

    t = threading.Thread(target=_poll, daemon=True, name=f"runpod-poll-{job_id[:8]}")
    t.start()
