import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(f"Invalid {name}='{raw}', using default {default}")
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(f"Invalid {name}='{raw}', using default {default}")
        return default


RUNPOD_AUTOSCALE_MIN_WORKERS = max(0, _env_int("RUNPOD_AUTOSCALE_MIN_WORKERS", 3))
RUNPOD_AUTOSCALE_MAX_WORKERS = max(
    RUNPOD_AUTOSCALE_MIN_WORKERS,
    _env_int("RUNPOD_AUTOSCALE_MAX_WORKERS", 4),
)
RUNPOD_AUTOSCALE_SAFETY_SWEEP_SEC = max(
    5.0,
    _env_float("RUNPOD_AUTOSCALE_SAFETY_SWEEP_SEC", 30.0),
)
# Jobs stuck in pending/running longer than this are force-failed by the safety sweep
RUNPOD_AUTOSCALE_STUCK_JOB_SEC = max(
    60.0,
    _env_float("RUNPOD_AUTOSCALE_STUCK_JOB_SEC", 1800.0),  # 30 min default
)

_configured_endpoint_ids: list[str] = []
_db_query_all = None
_configured = False
_safety_thread_started = False

_endpoint_scaled_up: dict[str, bool] = {}
_active_jobs_per_endpoint: dict[str, set[str]] = {}
_endpoint_names: dict[str, str] = {}    # endpoint_id -> name
_endpoint_gpu_ids: dict[str, list[str]] = {}  # endpoint_id -> gpuIds
_scale_lock = threading.Lock()


def _fetch_endpoint_configs(endpoint_ids: list[str]) -> dict[str, dict]:
    """Query RunPod for all fields required by saveEndpoint."""
    query = """
    query {
      myself {
        endpoints {
          id
          name
          gpuIds
        }
      }
    }
    """
    try:
        resp = httpx.post(
            f"https://api.runpod.io/graphql?api_key={RUNPOD_API_KEY}",
            json={"query": query},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            log.error(f"Failed to fetch endpoint configs: {data['errors']}")
            return {}
        endpoints = (data.get("data") or {}).get("myself", {}).get("endpoints") or []
        configs = {
            ep["id"]: ep
            for ep in endpoints
            if ep.get("id")
        }
        log.info(
            f"Fetched RunPod endpoint configs for: "
            f"{ {eid: cfg.get('name') for eid, cfg in configs.items() if eid in endpoint_ids} }"
        )
        return configs
    except Exception as exc:
        log.error(f"Failed to fetch endpoint configs: {exc}")
        return {}


def configure(endpoint_ids: list[str], db_query_all) -> None:
    global _configured, _db_query_all
    _db_query_all = db_query_all
    configs = _fetch_endpoint_configs(endpoint_ids)
    with _scale_lock:
        _configured_endpoint_ids[:] = list(endpoint_ids)
        for endpoint_id in endpoint_ids:
            _endpoint_scaled_up.setdefault(endpoint_id, False)
            _active_jobs_per_endpoint.setdefault(endpoint_id, set())
            cfg = configs.get(endpoint_id, {})
            _endpoint_names[endpoint_id] = cfg.get("name") or endpoint_id
            _endpoint_gpu_ids[endpoint_id] = cfg.get("gpuIds") or []
    _configured = True


def is_configured() -> bool:
    return bool(_configured and RUNPOD_API_KEY and _configured_endpoint_ids and _db_query_all)


def _graphql_save_endpoint(endpoint_id: str, workers_min: int, workers_max: int) -> None:
    name = _endpoint_names.get(endpoint_id, endpoint_id)
    gpu_ids = _endpoint_gpu_ids.get(endpoint_id, [])
    query = """
    mutation SaveEndpoint($input: EndpointInput!) {
      saveEndpoint(input: $input) {
        id
        name
        gpuIds
        workersMin
        workersMax
      }
    }
    """
    variables = {
        "input": {
            "id": endpoint_id,
            "name": name,
            "gpuIds": gpu_ids,
            "workersMin": workers_min,
            "workersMax": workers_max,
        }
    }
    resp = httpx.post(
        f"https://api.runpod.io/graphql?api_key={RUNPOD_API_KEY}",
        json={"query": query, "variables": variables},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"RunPod GraphQL error for endpoint {endpoint_id}: {data['errors']}")
    result = (data.get("data") or {}).get("saveEndpoint") or {}
    log.info(
        f"RunPod saveEndpoint OK: id={result.get('id')} name={result.get('name')!r} "
        f"gpuIds={result.get('gpuIds')} "
        f"workersMin={result.get('workersMin')} workersMax={result.get('workersMax')}"
    )


def scale_up(endpoint_id: str) -> None:
    if not is_configured():
        log.warning(
            f"scale_up called for {endpoint_id} but autoscaler not configured "
            f"(configured={_configured} api_key={bool(RUNPOD_API_KEY)} "
            f"endpoints={_configured_endpoint_ids} db={bool(_db_query_all)})"
        )
        return
    _graphql_save_endpoint(
        endpoint_id,
        RUNPOD_AUTOSCALE_MIN_WORKERS,
        RUNPOD_AUTOSCALE_MAX_WORKERS,
    )
    with _scale_lock:
        _endpoint_scaled_up[endpoint_id] = True
    log.info(
        f"RunPod autoscaler scaled up endpoint {endpoint_id} "
        f"to min={RUNPOD_AUTOSCALE_MIN_WORKERS} max={RUNPOD_AUTOSCALE_MAX_WORKERS}"
    )


def scale_down(endpoint_id: str) -> None:
    if not is_configured():
        return
    _graphql_save_endpoint(endpoint_id, 0, 0)
    with _scale_lock:
        _endpoint_scaled_up[endpoint_id] = False
    log.info(f"RunPod autoscaler scaled down endpoint {endpoint_id} to min=0 max=0")


def notify_job_started(job_id: str, endpoint_id: str) -> None:
    with _scale_lock:
        _active_jobs_per_endpoint.setdefault(endpoint_id, set()).add(job_id)


def notify_job_terminal(job_id: str, endpoint_id: str) -> None:
    with _scale_lock:
        _active_jobs_per_endpoint.setdefault(endpoint_id, set()).discard(job_id)
    maybe_scale_down(endpoint_id)


def _db_active_job_ids(endpoint_id: str) -> set[str]:
    if not is_configured():
        return set()
    rows = _db_query_all(
        """
        SELECT j.id
        FROM jobs j
        JOIN machines m ON j.machine_id = m.id
        WHERE m.machine_key = %s
          AND j.status IN ('pending', 'running')
        """,
        (f"runpod-serverless-{endpoint_id}",),
    )
    return {row["id"] for row in rows}


def maybe_scale_down(endpoint_id: str, force: bool = False) -> bool:
    if not is_configured():
        return False
    active_db_jobs = _db_active_job_ids(endpoint_id)
    with _scale_lock:
        active_jobs = set(_active_jobs_per_endpoint.setdefault(endpoint_id, set()))
        scaled_up = _endpoint_scaled_up.get(endpoint_id, False)
    if not scaled_up:
        return False
    if active_db_jobs:
        return False
    if active_jobs and not force:
        return False
    if active_jobs and force:
        with _scale_lock:
            _active_jobs_per_endpoint.setdefault(endpoint_id, set()).clear()
    try:
        scale_down(endpoint_id)
        return True
    except Exception as exc:
        log.error(f"RunPod autoscaler failed to scale down endpoint {endpoint_id}: {exc}")
        return False


def _reap_stuck_jobs(endpoint_id: str) -> int:
    """Force-fail jobs stuck in pending/running beyond the timeout.
    Returns the number of reaped jobs."""
    if not is_configured():
        return 0
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=RUNPOD_AUTOSCALE_STUCK_JOB_SEC)
    ).isoformat()
    rows = _db_query_all(
        """
        SELECT j.id
        FROM jobs j
        JOIN machines m ON j.machine_id = m.id
        WHERE m.machine_key = %s
          AND j.status IN ('pending', 'running')
          AND j.submitted_at < %s
        """,
        (f"runpod-serverless-{endpoint_id}", cutoff),
    )
    reaped = 0
    for row in rows:
        job_id = row["id"]
        try:
            from infrastructure.db import execute
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s AND status IN ('pending', 'running')",
                (
                    f"Reaped by autoscaler safety sweep (stuck >{RUNPOD_AUTOSCALE_STUCK_JOB_SEC:.0f}s)",
                    datetime.now(timezone.utc).isoformat(),
                    job_id,
                ),
            )
            reaped += 1
            log.warning(f"Autoscaler reaped stuck job {job_id} on endpoint {endpoint_id}")
        except Exception as exc:
            log.error(f"Failed to reap stuck job {job_id}: {exc}")
    with _scale_lock:
        _active_jobs_per_endpoint.setdefault(endpoint_id, set()).difference_update(
            {row["id"] for row in rows}
        )
    return reaped


def scale_down_all() -> None:
    if not is_configured():
        return
    for endpoint_id in list(_configured_endpoint_ids):
        try:
            scale_down(endpoint_id)
        except Exception as exc:
            log.error(f"RunPod autoscaler startup scale-down failed for {endpoint_id}: {exc}")


def start_safety_sweep() -> None:
    global _safety_thread_started
    if not is_configured() or _safety_thread_started:
        return

    def _loop():
        while True:
            time.sleep(RUNPOD_AUTOSCALE_SAFETY_SWEEP_SEC)
            for endpoint_id in list(_configured_endpoint_ids):
                try:
                    reaped = _reap_stuck_jobs(endpoint_id)
                    if reaped:
                        log.warning(
                            f"Safety sweep reaped {reaped} stuck job(s) on endpoint {endpoint_id}"
                        )
                    maybe_scale_down(endpoint_id, force=True)
                except Exception as exc:
                    log.error(f"RunPod autoscaler safety sweep error for {endpoint_id}: {exc}")

    thread = threading.Thread(target=_loop, daemon=True, name="runpod-autoscaler")
    thread.start()
    _safety_thread_started = True
    log.info(
        f"RunPod autoscaler safety sweep started "
        f"(interval={RUNPOD_AUTOSCALE_SAFETY_SWEEP_SEC:.0f}s)"
    )
