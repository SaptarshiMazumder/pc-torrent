"""
Vast.ai dispatch layer (multi-GPU-type).

Responsibilities:
- Register/maintain a virtual "vast_serverless" machine per GPU type profile
- Keep heartbeats alive so they stay visible in the available machines list
- Dispatch render jobs by renting a Vast.ai instance per job
- Poll the instance until it exits, then clean up and delegate
  retry/failover to DispatchCoordinator

Each dispatch creates a new Vast.ai instance with job parameters passed as
env vars. The instance runs the handler, calls back to the backend, then
exits. The polling thread detects completion and destroys the instance.

Env var format:
  VAST_API_KEY=your_key
  VAST_DOCKER_IMAGE=yourrepo/pc-rent-vast:latest
  VAST_INSTANCES=RTX 4090:Vast RTX 4090 24GB,H100 80GB SXM:Vast H100 80GB
  VAST_DISK_GB=20          (default: 20)
  VAST_MAX_PRICE_PER_GPU=0.50   ($/hr cap, default: 0.50)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from uuid import uuid4

import httpx

from domain.value_objects import now_iso
from infrastructure.db import execute, query_one

log = logging.getLogger(__name__)

VAST_API_BASE = "https://console.vast.ai/api/v0"


# ---------------------------------------------------------------------------
# Env helpers
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

VAST_API_KEY = os.getenv("VAST_API_KEY", "")
# Defaults to the same RunPod GHCR image — /vast_handler.py is baked in there.
# Override with VAST_DOCKER_IMAGE if you want to use a different tag.
VAST_DOCKER_IMAGE = os.getenv("VAST_DOCKER_IMAGE") or os.getenv("MODAL_WORKER_IMAGE", "")

VAST_GPU_VRAM_GB = _env_float("VAST_GPU_VRAM_GB", 24.0)
VAST_CPU_CORES = _env_int("VAST_CPU_CORES", 8)
VAST_RAM_GB = _env_float("VAST_RAM_GB", 32.0)
VAST_DISK_GB = _env_float("VAST_DISK_GB", 20.0)
VAST_MAX_PRICE_PER_GPU = _env_float("VAST_MAX_PRICE_PER_GPU", 0.50)

PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000")

POLL_INTERVAL_SEC = _env_float("VAST_POLL_INTERVAL_SEC", 15.0)
STARTUP_TIMEOUT_SEC = _env_float("VAST_STARTUP_TIMEOUT_SEC", 300.0)   # 5 min to become "running"
IN_PROGRESS_STALE_SEC = _env_float("IN_PROGRESS_STALE_SEC", 90 * 60)

try:
    VAST_WORKERS_PER_ENDPOINT = max(1, int(os.getenv("VAST_WORKERS_PER_ENDPOINT", "2")))
except ValueError:
    VAST_WORKERS_PER_ENDPOINT = 2

_HEARTBEAT_INTERVAL = 10


# ---------------------------------------------------------------------------
# Endpoint parsing
# ---------------------------------------------------------------------------

def _parse_endpoints() -> list[dict]:
    """
    VAST_INSTANCES=RTX 4090:Vast RTX 4090 24GB,H100 80GB SXM:Vast H100 80GB
    The part before the first ':' is the GPU model name used for offer searches.
    """
    raw = os.getenv("VAST_INSTANCES", "").strip()
    if not raw:
        return []

    endpoints = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            gpu_name, label = part.split(":", 1)
            endpoints.append({"id": gpu_name.strip(), "label": label.strip()})
        else:
            endpoints.append({"id": part, "label": f"Vast {part}"})
    return endpoints


ENDPOINTS = _parse_endpoints()

# machine_id -> gpu_name (endpoint "id")
_machine_endpoint_map: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return bool(VAST_API_KEY and VAST_DOCKER_IMAGE and ENDPOINTS)


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {VAST_API_KEY}", "Content-Type": "application/json"}


def _deactivate_stale_virtual_machines(active_machine_keys: list[str]) -> None:
    if not active_machine_keys:
        execute("UPDATE machines SET status = 'idle' WHERE machine_type = 'vast_serverless'")
        return
    placeholders = ", ".join(["%s"] * len(active_machine_keys))
    execute(
        f"""
        UPDATE machines
        SET status = 'idle'
        WHERE machine_type = 'vast_serverless'
          AND machine_key NOT IN ({placeholders})
        """,
        tuple(active_machine_keys),
    )


def register_virtual_machines() -> list[str]:
    """Upsert one virtual machine row per Vast.ai GPU type."""
    if not is_enabled():
        _machine_endpoint_map.clear()
        _deactivate_stale_virtual_machines([])
        log.info("Vast.ai not configured — skipping registration")
        return []

    _machine_endpoint_map.clear()
    machine_ids = []
    active_machine_keys = []
    now = now_iso()

    for ep in ENDPOINTS:
        gpu_name = ep["id"]
        label = ep["label"]
        machine_key = f"vast-serverless-{gpu_name.replace(' ', '_').lower()}"
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
                (label, VAST_GPU_VRAM_GB, VAST_CPU_CORES, VAST_RAM_GB,
                 "Linux", "vast_serverless", now, machine_id),
            )
            log.info(f"Vast.ai endpoint '{gpu_name}' updated: {machine_id} ({label})")
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
                 label, VAST_GPU_VRAM_GB, VAST_CPU_CORES, VAST_RAM_GB,
                 "Linux", "vast_serverless", now, now),
            )
            log.info(f"Vast.ai endpoint '{gpu_name}' registered: {machine_id} ({label})")

        _machine_endpoint_map[machine_id] = gpu_name
        machine_ids.append(machine_id)

    _deactivate_stale_virtual_machines(active_machine_keys)
    return machine_ids


def start_heartbeat_thread() -> None:
    if not is_enabled():
        return

    machine_keys = [
        f"vast-serverless-{ep['id'].replace(' ', '_').lower()}"
        for ep in ENDPOINTS
    ]

    def _loop():
        while True:
            try:
                for mk in machine_keys:
                    execute(
                        """
                        UPDATE machines
                        SET last_seen_at = %s
                        WHERE machine_key = %s AND machine_type = 'vast_serverless'
                        """,
                        (now_iso(), mk),
                    )
            except Exception as e:
                log.warning(f"Vast.ai heartbeat error: {e}")
            time.sleep(_HEARTBEAT_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="vast-heartbeat")
    t.start()
    log.info(f"Vast.ai heartbeat thread started ({len(ENDPOINTS)} endpoint(s))")


def _gpu_name_for_machine(machine_id: str) -> str:
    gpu_name = _machine_endpoint_map.get(machine_id)
    if gpu_name:
        return gpu_name
    if len(ENDPOINTS) == 1:
        return ENDPOINTS[0]["id"]
    raise ValueError(f"No Vast.ai endpoint mapped for machine_id={machine_id}")


# ---------------------------------------------------------------------------
# Vast.ai API calls
# ---------------------------------------------------------------------------

def search_offers(gpu_name: str) -> list[dict]:
    """Find the cheapest rentable single-GPU offers matching gpu_name."""
    query = json.dumps({
        "gpu_name": {"eq": gpu_name},
        "num_gpus": {"eq": 1},
        "rentable": {"eq": True},
        "dph_total": {"lte": VAST_MAX_PRICE_PER_GPU},
        "disk_space": {"gte": VAST_DISK_GB},
        "order": [["dph_total", "asc"]],
        "limit": 10,
    })
    resp = httpx.get(
        f"{VAST_API_BASE}/bundles/",
        headers=_auth_headers(),
        params={"q": query},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("offers", [])


def create_instance(
    offer_id: int,
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
) -> int:
    """Rent a Vast.ai instance from offer_id and start the render handler."""
    env_vars = {
        "-e JOB_ID": job_id,
        "-e BLEND_URL": blend_url,
        "-e FRAME_START": str(frame_start),
        "-e FRAME_END": str(frame_end),
        "-e FRAME_STEP": str(frame_step),
        "-e RENDER_OVERRIDES_B64": render_overrides_b64,
        "-e BACKEND_URL": PUBLIC_BACKEND_URL,
    }

    resp = httpx.put(
        f"{VAST_API_BASE}/asks/{offer_id}/",
        headers=_auth_headers(),
        json={
            "client_id": "me",
            "image": VAST_DOCKER_IMAGE,
            "env": env_vars,
            "disk": VAST_DISK_GB,
            "label": f"pcrent-{job_id[:12]}",
            # Override the RunPod image's default CMD so the RunPod SDK loop
            # never starts. /vast_handler.py is baked into the same image.
            "onstart": "python3 -u /vast_handler.py",
            "runtype": "args",
            "args_str": "sleep infinity",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    instance_id: int = data.get("new_contract") or data.get("id")
    if not instance_id:
        raise RuntimeError(f"Vast.ai create_instance returned no ID: {data}")
    return instance_id


def get_instance(instance_id: int) -> dict | None:
    """Fetch a single instance's status from Vast.ai."""
    resp = httpx.get(
        f"{VAST_API_BASE}/instances/{instance_id}/",
        headers=_auth_headers(),
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    # API returns {"instances": {...}} or the instance dict directly
    if "instances" in data:
        return data["instances"]
    return data


def destroy_instance(instance_id: int) -> None:
    """Destroy (delete) a Vast.ai instance."""
    try:
        resp = httpx.delete(
            f"{VAST_API_BASE}/instances/{instance_id}/",
            headers=_auth_headers(),
            timeout=15,
        )
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()
        log.info(f"Destroyed Vast.ai instance {instance_id}")
    except Exception as e:
        log.warning(f"Failed to destroy Vast.ai instance {instance_id}: {e}")


def dispatch_job(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
    machine_id: str = "",
) -> int:
    """Find a cheap offer and rent it. Returns the Vast.ai instance ID."""
    gpu_name = _gpu_name_for_machine(machine_id)
    offers = search_offers(gpu_name)
    if not offers:
        raise RuntimeError(
            f"No Vast.ai offers found for gpu_name='{gpu_name}' "
            f"under ${VAST_MAX_PRICE_PER_GPU}/hr with {VAST_DISK_GB}GB disk"
        )

    # Try offers in price order until one succeeds
    last_err: Exception | None = None
    for offer in offers:
        offer_id = offer.get("id")
        dph = offer.get("dph_total", "?")
        try:
            instance_id = create_instance(
                offer_id=offer_id,
                job_id=job_id,
                blend_url=blend_url,
                frame_start=frame_start,
                frame_end=frame_end,
                frame_step=frame_step,
                render_overrides_b64=render_overrides_b64,
            )
            log.info(
                f"Dispatched job {job_id} -> Vast.ai offer {offer_id} "
                f"(${dph}/hr) -> instance {instance_id}"
            )
            return instance_id
        except Exception as e:
            log.warning(f"Failed to rent Vast.ai offer {offer_id}: {e}")
            last_err = e

    raise RuntimeError(
        f"All Vast.ai offers for '{gpu_name}' failed. Last error: {last_err}"
    )


def cancel_job(provider_job_id: str, machine_id: str = "") -> None:
    """Destroy the Vast.ai instance (terminates the job)."""
    try:
        instance_id = int(provider_job_id)
        destroy_instance(instance_id)
    except (ValueError, TypeError) as e:
        log.warning(f"Invalid Vast.ai instance ID '{provider_job_id}': {e}")


def dispatch_and_save(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
    machine_id: str,
) -> str:
    """Dispatch to Vast.ai and persist the instance ID as the provider job ID."""
    instance_id = dispatch_job(
        job_id=job_id,
        blend_url=blend_url,
        frame_start=frame_start,
        frame_end=frame_end,
        frame_step=frame_step,
        render_overrides_b64=render_overrides_b64,
        machine_id=machine_id,
    )
    execute("UPDATE jobs SET runpod_job_id = %s WHERE id = %s", (str(instance_id), job_id))
    return str(instance_id)


def start_polling_thread(
    job_id: str,
    instance_id: str,
    machine_id: str = "",
    blend_url: str = "",
    render_overrides_b64: str = "",
    group_id: str = "",
) -> None:
    """
    Start a background thread that polls the Vast.ai instance status until
    it exits, then delegates retry/failover to DispatchCoordinator and
    destroys the instance.
    """
    vast_instance_id = int(instance_id)

    def _poll():
        from scheduling.dispatch_coordinator import coordinator

        started_at = time.monotonic()
        last_rendered_frames = None
        last_frame_change_at = time.monotonic()
        became_running_at: float | None = None

        while True:
            time.sleep(POLL_INTERVAL_SEC)
            try:
                inst = get_instance(vast_instance_id)
                if inst is None:
                    # Instance gone — check DB to decide if that's OK
                    job = query_one("SELECT status FROM jobs WHERE id = %s", (job_id,))
                    local_status = job["status"] if job else "unknown"
                    if local_status in ("done", "failed", "cancelled"):
                        log.info(f"Vast.ai instance {vast_instance_id} gone; job {job_id} is {local_status}")
                    else:
                        log.warning(f"Vast.ai instance {vast_instance_id} not found; job {job_id}={local_status}, marking failed")
                        if job:
                            coordinator.handle_failure(
                                job_id=job_id, job=job, error="Vast.ai instance disappeared unexpectedly",
                                blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                                failed_machine_id=machine_id, group_id=group_id,
                            )
                    break

                actual_status: str = str(inst.get("actual_status") or "").lower()
                elapsed = time.monotonic() - started_at

                job = query_one(
                    """
                    SELECT status, attempt, max_retries, frame_start, frame_end, frame_step,
                           rendered_frames, input_filename, render_overrides_json,
                           chunk_index, chunk_size_frames, priority
                    FROM jobs WHERE id = %s
                    """,
                    (job_id,),
                )
                if not job:
                    log.warning(f"Poll: job {job_id} not found in DB, stopping")
                    destroy_instance(vast_instance_id)
                    break

                local_status = job["status"]

                # Already terminal from worker callback
                if local_status in ("done", "failed", "cancelled"):
                    log.info(f"Job {job_id} is {local_status}; destroying Vast.ai instance {vast_instance_id}")
                    destroy_instance(vast_instance_id)
                    break

                # Startup timeout — instance not running within STARTUP_TIMEOUT_SEC
                if actual_status not in ("running",) and elapsed > STARTUP_TIMEOUT_SEC:
                    startup_err = (
                        f"Vast.ai instance {vast_instance_id} stuck in '{actual_status}' "
                        f"for {elapsed:.0f}s — timing out"
                    )
                    log.warning(f"Job {job_id}: {startup_err}")
                    destroy_instance(vast_instance_id)
                    coordinator.handle_failure(
                        job_id=job_id, job=job, error=startup_err,
                        blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                        failed_machine_id=machine_id, group_id=group_id,
                    )
                    break

                # Track when instance first became running
                if actual_status == "running" and became_running_at is None:
                    became_running_at = time.monotonic()
                    log.info(f"Vast.ai instance {vast_instance_id} running for job {job_id}")

                # IN_PROGRESS stale check
                if local_status == "running":
                    cur_frames = job.get("rendered_frames") or 0
                    if cur_frames != last_rendered_frames:
                        last_rendered_frames = cur_frames
                        last_frame_change_at = time.monotonic()
                    elif time.monotonic() - last_frame_change_at > IN_PROGRESS_STALE_SEC:
                        stale_err = (
                            f"Vast.ai job running but no new frames for "
                            f"{IN_PROGRESS_STALE_SEC / 60:.0f} min — cancelling"
                        )
                        log.warning(f"Job {job_id}: {stale_err}")
                        destroy_instance(vast_instance_id)
                        coordinator.handle_failure(
                            job_id=job_id, job=job, error=stale_err,
                            blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                            failed_machine_id=machine_id, group_id=group_id,
                        )
                        break

                # Instance exited — worker should have called back already
                if actual_status in ("exited", "stopped", "offline"):
                    exit_code = inst.get("exit_code")
                    log.info(
                        f"Vast.ai instance {vast_instance_id} exited "
                        f"(status={actual_status}, exit_code={exit_code}) for job {job_id}"
                    )
                    destroy_instance(vast_instance_id)

                    # Re-check DB after short pause (worker callback may be in flight)
                    time.sleep(5)
                    job = query_one("SELECT status FROM jobs WHERE id = %s", (job_id,))
                    local_status = job["status"] if job else "unknown"

                    if local_status in ("done", "failed", "cancelled"):
                        log.info(f"Job {job_id} already {local_status} after instance exit — OK")
                    else:
                        err = (
                            f"Vast.ai instance exited (status={actual_status}, "
                            f"exit_code={exit_code}) but job not completed"
                        )
                        log.warning(f"Job {job_id}: {err}")
                        if job:
                            coordinator.handle_failure(
                                job_id=job_id, job=job, error=err,
                                blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                                failed_machine_id=machine_id, group_id=group_id,
                            )
                    break

            except Exception as e:
                log.error(f"Vast.ai poll error for job {job_id}: {e}")

    t = threading.Thread(target=_poll, daemon=True, name=f"vast-poll-{job_id[:8]}")
    t.start()
