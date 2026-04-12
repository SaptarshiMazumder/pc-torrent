import collections
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from infrastructure.db import execute, query_all, query_one

log = logging.getLogger(__name__)

FAILURE_BUFFER_SIZE = 500
_failure_buffer: collections.deque[dict] = collections.deque(maxlen=FAILURE_BUFFER_SIZE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_start_iso(window_minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=max(1, window_minutes))).isoformat()


def record_failure(
    provider: str,
    endpoint_id: str,
    job_id: str,
    group_id: str,
    failure_type: str,
    error_msg: str,
    action_taken: str,
    reassigned_job_id: str | None = None,
    reassigned_to_endpoint: str | None = None,
) -> dict:
    event = {
        "id": str(uuid4()),
        "occurred_at": _now_iso(),
        "provider": provider,
        "endpoint_id": endpoint_id,
        "job_id": job_id,
        "group_id": group_id,
        "failure_type": failure_type,
        "error_msg": error_msg,
        "action_taken": action_taken,
        "reassigned_job_id": reassigned_job_id,
        "reassigned_to_endpoint": reassigned_to_endpoint,
        "resolved": False,
    }
    try:
        execute(
            """
            INSERT INTO failure_events (
                id, occurred_at, provider, endpoint_id, job_id, group_id,
                failure_type, error_msg, action_taken, reassigned_job_id,
                reassigned_to_endpoint, resolved
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event["id"],
                event["occurred_at"],
                event["provider"],
                event["endpoint_id"],
                event["job_id"],
                event["group_id"],
                event["failure_type"],
                event["error_msg"],
                event["action_taken"],
                event["reassigned_job_id"],
                event["reassigned_to_endpoint"],
                event["resolved"],
            ),
        )
    except Exception as exc:
        log.error(f"Failed to persist failure event for job {job_id}: {exc}")
    _failure_buffer.appendleft(event)

    message = (
        f"[FAILURE] provider={provider} endpoint={endpoint_id or 'n/a'} "
        f"job={job_id} type={failure_type} action={action_taken}"
    )
    if reassigned_job_id:
        message += f" new_job={reassigned_job_id}"
    if reassigned_to_endpoint:
        message += f" reassigned_to={reassigned_to_endpoint}"
    if error_msg:
        message += f' error="{error_msg}"'
    log.warning(message)
    return event


def get_recent_failures(
    limit: int = 100,
    provider: str | None = None,
    endpoint_id: str | None = None,
    failure_type: str | None = None,
) -> list[dict]:
    safe_limit = max(1, min(int(limit or 100), 500))
    filters = []
    params: list[object] = []
    if provider:
        filters.append("provider = %s")
        params.append(provider)
    if endpoint_id:
        filters.append("endpoint_id = %s")
        params.append(endpoint_id)
    if failure_type:
        filters.append("failure_type = %s")
        params.append(failure_type)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(safe_limit)
    return query_all(
        f"""
        SELECT *
        FROM failure_events
        {where_sql}
        ORDER BY occurred_at DESC
        LIMIT %s
        """,
        tuple(params),
    )


def endpoint_failure_rate(endpoint_id: str, window_minutes: int = 5) -> float:
    window_start = _window_start_iso(window_minutes)
    failure_row = query_one(
        """
        SELECT COUNT(*) AS failure_count
        FROM failure_events
        WHERE endpoint_id = %s AND occurred_at >= %s
        """,
        (endpoint_id, window_start),
    ) or {"failure_count": 0}
    total_row = query_one(
        """
        SELECT COUNT(*) AS total_count
        FROM jobs j
        JOIN machines m ON j.machine_id = m.id
        WHERE m.machine_key = %s
          AND j.submitted_at >= %s
        """,
        (endpoint_id, window_start),
    ) or {"total_count": 0}

    failures = int(failure_row.get("failure_count") or 0)
    total = int(total_row.get("total_count") or 0)
    if total <= 0:
        return 0.0
    return round(min(1.0, failures / total), 4)


def recent_failure_buffer(limit: int = 100) -> list[dict]:
    safe_limit = max(1, min(int(limit or 100), FAILURE_BUFFER_SIZE))
    return list(list(_failure_buffer)[:safe_limit])
