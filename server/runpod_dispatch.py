"""
RunPod serverless dispatch layer (multi-endpoint).

Responsibilities:
- Register/maintain a virtual "runpod_serverless" machine per endpoint
- Keep heartbeats alive so they stay visible in the marketplace
- Dispatch render jobs to the correct RunPod endpoint
- Poll RunPod /status/{id} until COMPLETED or FAILED and reconcile DB state

Env var formats (pick one):
  Multi-endpoint:  RUNPOD_ENDPOINTS=id1:Label One,id2:Label Two
  Single (legacy): RUNPOD_ENDPOINT_ID=id1
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
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")

# Specs reported to the marketplace (shared across all endpoints)
RUNPOD_GPU_VRAM_GB = _env_float("RUNPOD_GPU_VRAM_GB", 24.0)
RUNPOD_CPU_CORES = _env_int("RUNPOD_CPU_CORES", 16)
RUNPOD_RAM_GB = _env_float("RUNPOD_RAM_GB", 64.0)

# Public URL your server is reachable at from inside RunPod workers
PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000")

# How often to poll RunPod /status (seconds)
JOB_STATUS_POLL_INTERVAL_SEC = float(os.getenv("JOB_STATUS_POLL_INTERVAL_SEC", "5"))

# Heartbeat cadence for virtual machine rows (seconds)
_HEARTBEAT_INTERVAL = 10


# ---------------------------------------------------------------------------
# Endpoint parsing
# ---------------------------------------------------------------------------

def _parse_endpoints() -> list[dict]:
    """
    Parse RunPod endpoints from env vars.

    Multi-endpoint format (preferred):
        RUNPOD_ENDPOINTS=id1:Label 1,id2:Label 2

    Legacy single-endpoint format:
        RUNPOD_ENDPOINT_ID=id1
        RUNPOD_GPU_MODEL=Label 1        (optional, for display name)
    """
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

    # Legacy single endpoint
    eid = os.getenv("RUNPOD_ENDPOINT_ID", "").strip()
    if eid:
        label = os.getenv("RUNPOD_GPU_MODEL", "RunPod Serverless (RTX 4090)")
        return [{"id": eid, "label": label}]

    return []


ENDPOINTS = _parse_endpoints()

# machine_id -> endpoint_id (populated during registration)
_machine_endpoint_map: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return bool(RUNPOD_API_KEY and ENDPOINTS)


def _deactivate_stale_virtual_machines(db_execute, active_machine_keys: list[str]) -> None:
    """
    Keep only currently configured RunPod virtual machines visible.
    Any legacy/removed endpoint rows are marked idle so /machines excludes them.
    """
    if not active_machine_keys:
        db_execute(
            """
            UPDATE machines
            SET status = 'idle'
            WHERE machine_type = 'runpod_serverless'
            """
        )
        return

    placeholders = ", ".join(["%s"] * len(active_machine_keys))
    db_execute(
        f"""
        UPDATE machines
        SET status = 'idle'
        WHERE machine_type = 'runpod_serverless'
          AND machine_key NOT IN ({placeholders})
        """,
        tuple(active_machine_keys),
    )


def register_virtual_machines(db_execute, db_query_one, now_iso) -> list[str]:
    """
    Upsert one virtual machine row per RunPod endpoint.
    Returns list of machine_ids, or empty list if RunPod is not configured.
    """
    if not is_enabled():
        _machine_endpoint_map.clear()
        _deactivate_stale_virtual_machines(db_execute, [])
        log.info("RunPod not configured (RUNPOD_API_KEY / RUNPOD_ENDPOINTS unset) - skipping")
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
                    label, RUNPOD_GPU_VRAM_GB, RUNPOD_CPU_CORES, RUNPOD_RAM_GB,
                    "Linux", "runpod_serverless", now, machine_id,
                ),
            )
            log.info(f"RunPod endpoint {endpoint_id} updated: {machine_id} ({label})")
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
                    label, RUNPOD_GPU_VRAM_GB, RUNPOD_CPU_CORES, RUNPOD_RAM_GB,
                    "Linux", "runpod_serverless", now, now,
                ),
            )
            log.info(f"RunPod endpoint {endpoint_id} registered: {machine_id} ({label})")

        _machine_endpoint_map[machine_id] = endpoint_id
        machine_ids.append(machine_id)

    _deactivate_stale_virtual_machines(db_execute, active_machine_keys)
    return machine_ids


def start_heartbeat_thread(db_execute, now_iso):
    """
    Background thread: refreshes last_seen_at for all RunPod virtual
    machines every _HEARTBEAT_INTERVAL seconds.
    """
    if not is_enabled():
        return

    machine_keys = [f"runpod-serverless-{ep['id']}" for ep in ENDPOINTS]

    def _loop():
        while True:
            try:
                for mk in machine_keys:
                    db_execute(
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
    """Resolve which RunPod endpoint a machine_id maps to."""
    eid = _machine_endpoint_map.get(machine_id)
    if eid:
        return eid
    # Fallback for single-endpoint setups
    if len(ENDPOINTS) == 1:
        return ENDPOINTS[0]["id"]
    raise ValueError(f"No RunPod endpoint mapped for machine_id={machine_id}")


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
    POST a job to the correct RunPod serverless endpoint.
    Returns the RunPod job ID.
    Raises on failure.
    """
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


def start_polling_thread(
    job_id: str,
    runpod_job_id: str,
    db_execute,
    db_query_one,
    now_iso,
    machine_id: str = "",
):
    """
    Background thread: polls RunPod /status/{runpod_job_id} every
    JOB_STATUS_POLL_INTERVAL_SEC seconds.
    """
    endpoint_id = (
        _endpoint_id_for_machine(machine_id)
        if machine_id
        else ENDPOINTS[0]["id"]
    )

    def _poll():
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

                # IN_QUEUE or unknown -> keep polling

            except Exception as e:
                log.error(f"RunPod poll error for job {job_id}: {e}")
                # keep retrying - transient network issues shouldn't kill the loop

    t = threading.Thread(target=_poll, daemon=True, name=f"runpod-poll-{job_id[:8]}")
    t.start()
