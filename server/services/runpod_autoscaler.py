import logging
import os
import threading
import time

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


RUNPOD_AUTOSCALE_MIN_WORKERS = max(0, _env_int("RUNPOD_AUTOSCALE_MIN_WORKERS", 2))
RUNPOD_AUTOSCALE_MAX_WORKERS = max(
    RUNPOD_AUTOSCALE_MIN_WORKERS,
    _env_int("RUNPOD_AUTOSCALE_MAX_WORKERS", 4),
)
RUNPOD_AUTOSCALE_SAFETY_SWEEP_SEC = max(
    5.0,
    _env_float("RUNPOD_AUTOSCALE_SAFETY_SWEEP_SEC", 60.0),
)

_configured_endpoint_ids: list[str] = []
_db_query_all = None
_configured = False
_safety_thread_started = False

_endpoint_scaled_up: dict[str, bool] = {}
_active_jobs_per_endpoint: dict[str, set[str]] = {}
_scale_lock = threading.Lock()


def configure(endpoint_ids: list[str], db_query_all) -> None:
    global _configured, _db_query_all
    _db_query_all = db_query_all
    with _scale_lock:
        _configured_endpoint_ids[:] = list(endpoint_ids)
        for endpoint_id in endpoint_ids:
            _endpoint_scaled_up.setdefault(endpoint_id, False)
            _active_jobs_per_endpoint.setdefault(endpoint_id, set())
    _configured = True


def is_configured() -> bool:
    return bool(_configured and RUNPOD_API_KEY and _configured_endpoint_ids and _db_query_all)


def _graphql_save_endpoint(endpoint_id: str, workers_min: int, workers_max: int) -> None:
    query = """
    mutation SaveEndpoint($input: EndpointInput!) {
      saveEndpoint(input: $input) {
        id
        workersMin
        workersMax
        flashBootType
      }
    }
    """
    variables = {
        "input": {
            "id": endpoint_id,
            "workersMin": workers_min,
            "workersMax": workers_max,
            "flashBootType": "FLASHBOOT",
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
        raise RuntimeError(str(data["errors"]))


def scale_up(endpoint_id: str) -> None:
    if not is_configured():
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
