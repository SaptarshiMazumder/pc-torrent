"""Modal-specific job state helpers.

Keeps Modal provider identifiers isolated from other providers so shared
code can ask the Modal service for its own state without reusing RunPod/Vast
field names.
"""

from __future__ import annotations

from infrastructure.db import execute


def save_function_call_id(job_id: str, function_call_id: str) -> None:
    execute(
        "UPDATE jobs SET modal_function_call_id = %s WHERE id = %s",
        (function_call_id, job_id),
    )


def function_call_id_from_job(job: dict | None) -> str | None:
    if not job:
        return None
    value = (job.get("modal_function_call_id") or "").strip()
    return value or None
