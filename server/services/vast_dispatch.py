"""
Vast.ai dispatch layer (multi-GPU-type).

Responsibilities:
- Register/maintain a virtual "vast_serverless" machine per GPU type profile
- Keep heartbeats alive so they stay visible in the available machines list
- Dispatch render jobs by renting a Vast.ai instance per job
- Poll the instance until it exits, then clean up and handle failover
- On failure: immediately move to a different machine (no same-host retry),
  trying all available candidates until one accepts the dispatch.

Each dispatch creates a new Vast.ai instance with job parameters passed as
env vars. The instance runs the handler, calls back to the backend, then
exits. The polling thread detects completion and destroys the instance.

GPU endpoints are configured in server/config.json under "vast_instances".

Env var format:
  VAST_API_KEY=your_key
  VAST_DOCKER_IMAGE=yourrepo/pc-rent-vast:latest
  VAST_DISK_GB=20          (default: 20)
  VAST_MAX_PRICE_PER_GPU=1.00   ($/hr cap, default: 1.00)
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
from infrastructure.db import execute, query_all, query_one

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
VAST_DOCKER_IMAGE = os.getenv("VAST_DOCKER_IMAGE") or os.getenv("MODAL_WORKER_IMAGE", "")

VAST_CPU_CORES = _env_int("VAST_CPU_CORES", 8)
VAST_RAM_GB = _env_float("VAST_RAM_GB", 32.0)
VAST_DISK_GB = _env_float("VAST_DISK_GB", 20.0)
VAST_MAX_PRICE_PER_GPU = _env_float("VAST_MAX_PRICE_PER_GPU", 1.00)

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
# Endpoint parsing — reads from server/config.json
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")


def _parse_endpoints() -> list[dict]:
    """Load Vast.ai GPU endpoints from config.json -> vast_instances[].

    Each entry may include:
      gpu_name  (required) — the Vast.ai GPU name used for offer search
      label     (optional) — human-readable display name
      vram_gb   (optional) — GPU VRAM; used for power-score registration
    """
    try:
        with open(_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning(f"Could not load {_CONFIG_PATH}: {exc}")
        return []

    endpoints = []
    for entry in cfg.get("vast_instances", []):
        gpu_name = entry.get("gpu_name", "").strip()
        if not gpu_name:
            continue
        label = entry.get("label", "").strip() or f"Vast {gpu_name}"
        vram_gb = float(entry.get("vram_gb", 24))
        endpoints.append({"id": gpu_name, "label": label, "vram_gb": vram_gb})
    return endpoints


ENDPOINTS = _parse_endpoints()

# machine_id -> gpu_name (endpoint "id")
_machine_endpoint_map: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Per-instance state registry (in-memory, updated by polling threads)
# ---------------------------------------------------------------------------
# job_id -> state dict
_instance_registry: dict[str, dict] = {}
_registry_lock = threading.Lock()

MAX_LOG_LINES = 200  # keep last N lines per instance


def _registry_set(job_id: str, updates: dict) -> None:
    with _registry_lock:
        entry = _instance_registry.setdefault(job_id, {})
        entry.update(updates)


def _registry_remove(job_id: str) -> None:
    with _registry_lock:
        _instance_registry.pop(job_id, None)


def get_instance_states() -> list[dict]:
    """Return a snapshot of all currently tracked Vast.ai instance states."""
    with _registry_lock:
        return [dict(v) for v in _instance_registry.values()]


def get_instance_logs(instance_id: int) -> str:
    """Fetch recent logs from a running Vast.ai instance."""
    try:
        resp = httpx.get(
            f"{VAST_API_BASE}/instances/request_logs/{instance_id}/",
            headers=_auth_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Vast returns {"result": "log text"} or {"logs": "..."}
            return data.get("result") or data.get("logs") or ""
        return ""
    except Exception as e:
        log.debug(f"Failed to fetch logs for instance {instance_id}: {e}")
        return ""


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
        vram_gb = ep["vram_gb"]
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
                (label, vram_gb, VAST_CPU_CORES, VAST_RAM_GB,
                 "Linux", "vast_serverless", now, machine_id),
            )
            log.info(f"Vast.ai endpoint '{gpu_name}' updated: {machine_id} ({label}, {vram_gb}GB)")
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
                 label, vram_gb, VAST_CPU_CORES, VAST_RAM_GB,
                 "Linux", "vast_serverless", now, now),
            )
            log.info(f"Vast.ai endpoint '{gpu_name}' registered: {machine_id} ({label}, {vram_gb}GB)")

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
                        SET status = 'available', last_seen_at = %s
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


def recover_polling_threads() -> None:
    """
    On server startup, find all Vast jobs that were left in running/pending state
    (polling threads died with the previous process) and resume watching them.

    For each such job:
    - If the Vast instance still exists → restart the polling thread.
    - If the Vast instance is gone → mark the job failed and trigger failover.
    """
    if not is_enabled():
        return

    rows = query_all(
        """
        SELECT j.id, j.runpod_job_id, j.machine_id, j.group_id,
               j.frame_start, j.frame_end, j.frame_step,
               j.rendered_frames, j.render_overrides_json, j.input_filename,
               rg.input_filename AS rg_input_filename
        FROM jobs j
        LEFT JOIN render_groups rg ON rg.id = j.group_id
        WHERE j.status IN ('running', 'pending')
          AND j.machine_id IN (
              SELECT id FROM machines WHERE machine_type = 'vast_serverless'
          )
          AND j.runpod_job_id IS NOT NULL
        """,
        (),
    )

    if not rows:
        log.info("Vast recovery: no in-flight jobs to recover")
        return

    log.info(f"Vast recovery: found {len(rows)} in-flight job(s), checking instances...")

    import base64
    for row in rows:
        job_id       = row["id"]
        instance_id  = row["runpod_job_id"]
        machine_id   = row["machine_id"]
        group_id     = row["group_id"] or ""
        overrides_json = row.get("render_overrides_json") or "{}"
        render_overrides_b64 = base64.b64encode(overrides_json.encode()).decode()

        # Reconstruct blend_url from group input filename
        fname = row.get("rg_input_filename") or row.get("input_filename") or ""
        blend_url = f"{PUBLIC_BACKEND_URL}/render-groups/{group_id}/input/{fname}" if group_id and fname else ""

        try:
            vast_id = int(instance_id)
        except (ValueError, TypeError):
            log.warning(f"Vast recovery: job {job_id} has invalid instance_id '{instance_id}', marking failed")
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                ("Server restarted — instance ID invalid", now_iso(), job_id),
            )
            continue

        inst = get_instance(vast_id)
        if inst is None:
            log.warning(f"Vast recovery: instance {vast_id} for job {job_id} is gone — marking failed")
            job = query_one(
                "SELECT status, frame_start, frame_end, frame_step, rendered_frames, "
                "input_filename, render_overrides_json, chunk_index, chunk_size_frames, priority "
                "FROM jobs WHERE id = %s",
                (job_id,),
            )
            if job and blend_url:
                execute(
                    "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                    ("Instance gone after server restart", now_iso(), job_id),
                )
                handle_vast_failure(
                    job_id=job_id, job=job,
                    error="Instance gone after server restart",
                    blend_url=blend_url,
                    render_overrides_b64=render_overrides_b64,
                    failed_machine_id=machine_id,
                    group_id=group_id,
                )
            else:
                execute(
                    "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                    ("Instance gone after server restart — no blend_url to retry", now_iso(), job_id),
                )
        else:
            actual_status = str(inst.get("actual_status") or "").lower()
            log.info(f"Vast recovery: reattaching poll thread for job {job_id} instance {vast_id} ({actual_status})")
            start_polling_thread(
                job_id=job_id,
                instance_id=str(vast_id),
                machine_id=machine_id,
                blend_url=blend_url,
                render_overrides_b64=render_overrides_b64,
                group_id=group_id,
            )

    log.info("Vast recovery: done")


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

def _search_offers_query(gpu_name: str, *, verified_only: bool) -> list[dict]:
    """Run a single Vast.ai offer search for a specific GPU type.

    No price cap is applied — offers are returned sorted cheapest-first so the
    caller can pick the most economical available instance.
    """
    filters: dict = {
        "gpu_name": {"eq": gpu_name},
        "num_gpus": {"eq": 1},
        "rentable": {"eq": True},
        "reliability2": {"gte": 0.90},
        "cuda_max_good": {"gte": 12.0},
        "dph_total": {"lte": VAST_MAX_PRICE_PER_GPU},
        "disk_space": {"gte": VAST_DISK_GB},
        "order": [["dph_total", "asc"], ["reliability2", "desc"]],
        "limit": 10,
    }
    if verified_only:
        filters["verified"] = {"eq": True}
    resp = httpx.get(
        f"{VAST_API_BASE}/bundles/",
        headers=_auth_headers(),
        params={"q": json.dumps(filters)},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("offers", [])


def search_offers(gpu_name: str) -> list[dict]:
    """Find reliable rentable offers; prefer verified hosts, fall back to all."""
    offers = _search_offers_query(gpu_name, verified_only=True)
    if offers:
        log.info(f"Vast.ai: found {len(offers)} verified offers for {gpu_name}")
        return offers
    log.info(f"Vast.ai: no verified offers for {gpu_name}, trying unverified")
    return _search_offers_query(gpu_name, verified_only=False)



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
        "JOB_ID": job_id,
        "BLEND_URL": blend_url,
        "FRAME_START": str(frame_start),
        "FRAME_END": str(frame_end),
        "FRAME_STEP": str(frame_step),
        "RENDER_OVERRIDES_B64": render_overrides_b64,
        "BACKEND_URL": PUBLIC_BACKEND_URL,
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
            "runtype": "args",
            "args": ["python3", "-u", "/vast_handler.py"],
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
    """Find a cheap offer and rent it. Returns the Vast.ai instance ID.

    Tries the GPU type assigned to machine_id first.  If no offers are found
    under the price cap (${VAST_MAX_PRICE_PER_GPU}/hr), falls back through
    every other configured GPU type in order until one succeeds.
    """
    primary_gpu = _gpu_name_for_machine(machine_id)
    all_gpu_names = [ep["id"] for ep in ENDPOINTS]

    # Build priority list: primary GPU first, then all others in config order
    gpu_order = [primary_gpu] + [g for g in all_gpu_names if g != primary_gpu]

    last_err: Exception | None = None
    for gpu_name in gpu_order:
        offers = search_offers(gpu_name)
        if not offers:
            log.info(
                f"Vast.ai: no offers for '{gpu_name}' "
                f"under ${VAST_MAX_PRICE_PER_GPU}/hr — trying next GPU type"
            )
            continue

        # Try each offer for this GPU type in price order
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
                if gpu_name != primary_gpu:
                    log.info(
                        f"Job {job_id}: rented fallback GPU '{gpu_name}' "
                        f"(primary '{primary_gpu}' had no offers)"
                    )
                log.info(
                    f"Dispatched job {job_id} -> Vast.ai offer {offer_id} "
                    f"({gpu_name}, ${dph}/hr) -> instance {instance_id}"
                )
                return instance_id
            except Exception as e:
                log.warning(f"Failed to rent Vast.ai offer {offer_id} ({gpu_name}): {e}")
                last_err = e

    raise RuntimeError(
        f"No Vast.ai offers available across all {len(gpu_order)} GPU types "
        f"under ${VAST_MAX_PRICE_PER_GPU}/hr. Last error: {last_err}"
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


def handle_vast_failure(
    job_id: str,
    job: dict,
    error: str,
    blend_url: str,
    render_overrides_b64: str,
    failed_machine_id: str,
    group_id: str,
) -> str | None:
    """
    Vast-specific failure handler — does NOT retry on the same host.

    Marks the original job failed, then walks all available machines in
    VRAM-ranked order (serverless first) until a dispatch succeeds.  Each
    candidate gets one attempt; if it fails we log, mark that job failed,
    and move to the next one.  Only gives up when every candidate is
    exhausted.

    Returns the new job_id of the successful failover, or None if all fail.
    """
    from scheduling.dispatch_coordinator import coordinator

    rendered = max(0, job.get("rendered_frames") or 0)
    step = job.get("frame_step") or 1
    remaining_start = job["frame_start"] + rendered * step
    remaining_end = job["frame_end"]

    if not blend_url or remaining_start > remaining_end:
        execute(
            "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
            (now_iso(), error, job_id),
        )
        log.error(f"Vast job {job_id} failed with no frames left to migrate: {error}")
        return None

    # Mark original job failed before creating the replacement
    execute(
        "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
        (now_iso(), f"Failed, migrating remaining frames ({error})", job_id),
    )

    # Only failover to other Vast machines — not RunPod/Modal/community.
    # Vast failure handling is self-contained; other providers have their own billing
    # and lifecycle and should not be dragged in silently.
    rows = query_all(
        """
        SELECT * FROM machines
        WHERE status = 'available'
          AND machine_type = 'vast_serverless'
          AND id != %s
        ORDER BY gpu_vram_gb DESC
        """,
        (failed_machine_id,),
    )
    if not rows:
        log.error(f"Vast job {job_id}: no other Vast machines available for failover, frames lost")
        return None

    candidates = rows

    new_total = ((remaining_end - remaining_start) // step) + 1

    for candidate in candidates:
        new_job_id = str(uuid4())
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, group_id, input_filename, status,
                total_frames, rendered_frames, output_files,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, chunk_size_frames, submitted_at
            )
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s, 0, %s, %s, %s, %s, %s)
            """,
            (
                new_job_id, candidate["id"], group_id, job.get("input_filename"),
                new_total, remaining_start, remaining_end, step,
                job.get("render_overrides_json") or "{}",
                job.get("max_retries") or 0,
                job.get("priority") or 0,
                job.get("chunk_index"),
                job.get("chunk_size_frames"),
                now_iso(),
            ),
        )

        gpu_label = candidate.get("gpu_model", "?")
        machine_type = candidate.get("machine_type", "windows")
        log.info(
            f"Vast job {job_id} → failover attempt: new job {new_job_id} "
            f"on {gpu_label} ({machine_type})"
        )

        try:
            coordinator.dispatch(
                job_id=new_job_id,
                machine_id=candidate["id"],
                machine_type=machine_type,
                blend_url=blend_url,
                frame_start=remaining_start,
                frame_end=remaining_end,
                frame_step=step,
                render_overrides_b64=render_overrides_b64,
                group_id=group_id,
            )
            log.info(f"Vast failover succeeded: job {new_job_id} on {gpu_label}")
            return new_job_id
        except Exception as exc:
            log.warning(
                f"Vast failover dispatch failed for {new_job_id} on {gpu_label}: {exc} "
                f"— trying next candidate"
            )
            execute(
                "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
                (now_iso(), f"Failover dispatch failed: {exc}", new_job_id),
            )

    log.error(f"Vast job {job_id}: all failover candidates exhausted, frames lost")
    return None


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
    it exits, then handles failover via handle_vast_failure and destroys
    the instance. Also maintains the in-memory instance registry for the
    desktop live-monitoring UI.
    """
    vast_instance_id = int(instance_id)

    _JOB_QUERY = """
        SELECT status, attempt, max_retries, frame_start, frame_end, frame_step,
               rendered_frames, input_filename, render_overrides_json,
               chunk_index, chunk_size_frames, priority
        FROM jobs WHERE id = %s
    """

    # Seed registry entry immediately so the UI shows it right away
    _registry_set(job_id, {
        "job_id": job_id,
        "instance_id": vast_instance_id,
        "machine_id": machine_id,
        "group_id": group_id,
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

    def _poll():
        started_at = time.monotonic()
        last_rendered_frames = None
        last_frame_change_at = time.monotonic()
        became_running_at: float | None = None
        prev_actual_status: str = "created"

        # Fetch logs every Nth poll cycle (logs are slower / larger)
        LOG_FETCH_EVERY = 2
        poll_count = 0

        while True:
            time.sleep(POLL_INTERVAL_SEC)
            poll_count += 1
            try:
                inst = get_instance(vast_instance_id)
                elapsed = time.monotonic() - started_at

                if inst is None:
                    job = query_one(_JOB_QUERY, (job_id,))
                    local_status = job["status"] if job else "unknown"
                    _registry_set(job_id, {
                        "actual_status": "gone",
                        "job_status": local_status,
                        "elapsed_sec": int(elapsed),
                        "last_poll_at": now_iso(),
                    })
                    if local_status in ("done", "failed", "cancelled"):
                        log.info(f"Vast.ai instance {vast_instance_id} gone; job {job_id} is {local_status}")
                    else:
                        log.warning(
                            f"Vast.ai instance {vast_instance_id} not found; "
                            f"job {job_id}={local_status}, failing over"
                        )
                        if job:
                            handle_vast_failure(
                                job_id=job_id, job=job,
                                error="Vast.ai instance disappeared unexpectedly",
                                blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                                failed_machine_id=machine_id, group_id=group_id,
                            )
                    _registry_remove(job_id)
                    break

                actual_status: str = str(inst.get("actual_status") or "").lower()
                status_msg: str = str(inst.get("status_msg") or "").lower()

                # Detect container startup errors immediately — Vast never transitions
                # actual_status to "exited" for OCI/CDI/runtime errors; the instance
                # stays in "loading" indefinitely with the error in status_msg.
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
                if (
                    actual_status not in ("running", "exited", "stopped", "offline")
                    and any(frag in status_msg for frag in _FATAL_MSG_FRAGMENTS)
                ):
                    fatal_err = (
                        f"Vast.ai instance {vast_instance_id} fatal startup error "
                        f"(status={actual_status}): {inst.get('status_msg', '')[:200]}"
                    )
                    log.warning(f"Job {job_id}: {fatal_err}")
                    _registry_set(job_id, {"error": fatal_err, "actual_status": "error"})
                    destroy_instance(vast_instance_id)
                    job = query_one(_JOB_QUERY, (job_id,))
                    if job:
                        handle_vast_failure(
                            job_id=job_id, job=job, error=fatal_err,
                            blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                            failed_machine_id=machine_id, group_id=group_id,
                        )
                    _registry_remove(job_id)
                    break

                job = query_one(_JOB_QUERY, (job_id,))
                if not job:
                    log.warning(f"Poll: job {job_id} not found in DB, stopping")
                    destroy_instance(vast_instance_id)
                    _registry_remove(job_id)
                    break

                local_status = job["status"]

                # Fetch logs every cycle when not yet running (to catch startup errors fast),
                # then every Nth cycle once running (logs are larger)
                logs = ""
                if actual_status != "running" or poll_count % LOG_FETCH_EVERY == 0:
                    logs = get_instance_logs(vast_instance_id)

                # Build status_history entry if status changed
                history_entry = None
                if actual_status != prev_actual_status:
                    history_entry = {"status": actual_status, "at": now_iso()}
                    prev_actual_status = actual_status

                # Update registry
                update: dict = {
                    "actual_status": actual_status,
                    "job_status": local_status,
                    "gpu_model": inst.get("gpu_name") or inst.get("gpu_display_model") or None,
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
                    # Keep last MAX_LOG_LINES lines
                    update["logs"] = "\n".join(logs.splitlines()[-MAX_LOG_LINES:])
                if history_entry:
                    with _registry_lock:
                        entry = _instance_registry.setdefault(job_id, {})
                        hist = entry.get("status_history", [])
                        hist.append(history_entry)
                        entry["status_history"] = hist[-50:]  # cap at 50 entries
                _registry_set(job_id, update)

                # Already terminal from worker callback
                if local_status in ("done", "failed", "cancelled"):
                    log.info(f"Job {job_id} is {local_status}; destroying Vast.ai instance {vast_instance_id}")
                    destroy_instance(vast_instance_id)
                    _registry_remove(job_id)
                    break

                # Startup timeout
                if actual_status not in ("running",) and elapsed > STARTUP_TIMEOUT_SEC:
                    startup_err = (
                        f"Vast.ai instance {vast_instance_id} stuck in '{actual_status}' "
                        f"for {elapsed:.0f}s — timing out"
                    )
                    log.warning(f"Job {job_id}: {startup_err}")
                    _registry_set(job_id, {"error": startup_err})
                    destroy_instance(vast_instance_id)
                    handle_vast_failure(
                        job_id=job_id, job=job, error=startup_err,
                        blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                        failed_machine_id=machine_id, group_id=group_id,
                    )
                    _registry_remove(job_id)
                    break

                # Track when instance first became running
                if actual_status == "running" and became_running_at is None:
                    became_running_at = time.monotonic()
                    log.info(f"Vast.ai instance {vast_instance_id} running for job {job_id}")
                    if local_status == "pending":
                        execute(
                            "UPDATE jobs SET status = 'running' WHERE id = %s AND status = 'pending'",
                            (job_id,),
                        )
                        log.info(f"Job {job_id}: auto-transitioned pending → running")

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
                        _registry_set(job_id, {"error": stale_err})
                        destroy_instance(vast_instance_id)
                        handle_vast_failure(
                            job_id=job_id, job=job, error=stale_err,
                            blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                            failed_machine_id=machine_id, group_id=group_id,
                        )
                        _registry_remove(job_id)
                        break

                # Instance exited
                if actual_status in ("exited", "stopped", "offline"):
                    exit_code = inst.get("exit_code")
                    log.info(
                        f"Vast.ai instance {vast_instance_id} exited "
                        f"(status={actual_status}, exit_code={exit_code}) for job {job_id}"
                    )
                    _registry_set(job_id, {
                        "actual_status": actual_status,
                        "exit_code": exit_code,
                        "last_poll_at": now_iso(),
                    })
                    destroy_instance(vast_instance_id)

                    EXIT_CALLBACK_WAIT_SEC = 60
                    EXIT_POLL_SEC = 5
                    local_status = "unknown"
                    job = None
                    for _ in range(max(1, EXIT_CALLBACK_WAIT_SEC // EXIT_POLL_SEC)):
                        time.sleep(EXIT_POLL_SEC)
                        job = query_one(_JOB_QUERY, (job_id,))
                        local_status = job["status"] if job else "unknown"
                        _registry_set(job_id, {
                            "job_status": local_status,
                            "last_poll_at": now_iso(),
                        })
                        if local_status in ("done", "failed", "cancelled"):
                            break
                        log.debug(
                            f"Job {job_id} still '{local_status}' after instance exit, "
                            f"waiting for callback..."
                        )

                    if local_status in ("done", "failed", "cancelled"):
                        log.info(f"Job {job_id} is {local_status} after instance exit — OK")
                    else:
                        err = (
                            f"Vast.ai instance exited (status={actual_status}, "
                            f"exit_code={exit_code}) — callback did not arrive within "
                            f"{EXIT_CALLBACK_WAIT_SEC}s"
                        )
                        log.warning(f"Job {job_id}: {err}")
                        _registry_set(job_id, {"error": err})
                        if job:
                            handle_vast_failure(
                                job_id=job_id, job=job, error=err,
                                blend_url=blend_url, render_overrides_b64=render_overrides_b64,
                                failed_machine_id=machine_id, group_id=group_id,
                            )
                    _registry_remove(job_id)
                    break

            except Exception as e:
                log.error(f"Vast.ai poll error for job {job_id}: {e}")
                _registry_set(job_id, {
                    "last_poll_at": now_iso(),
                    "poll_error": str(e),
                })

    t = threading.Thread(target=_poll, daemon=True, name=f"vast-poll-{job_id[:8]}")
    t.start()
