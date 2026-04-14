"""Pure function: compute overall group status from per-job statuses."""

from __future__ import annotations

from serverV2.core.models import GroupStatusResult

_ACTIVE = frozenset({"pending", "running"})
_TERMINAL_GROUP = frozenset({"cancelled", "failed"})


def compute_group_status(
    *,
    current_group_status: str,
    job_statuses: list[str],
    total_frames: int,
    total_rendered: int,
) -> GroupStatusResult:
    no_active = not any(s in _ACTIVE for s in job_statuses)
    all_frames = total_frames > 0 and total_rendered >= total_frames
    any_done = any(s == "done" for s in job_statuses)
    all_done = bool(job_statuses) and all(s == "done" for s in job_statuses)
    all_failed = bool(job_statuses) and all(s == "failed" for s in job_statuses)
    any_running = any(s == "running" for s in job_statuses)

    if current_group_status in _TERMINAL_GROUP:
        if no_active and any_done and all_frames:
            return GroupStatusResult(status="done", should_persist=current_group_status != "done")
        return GroupStatusResult(status=current_group_status, should_persist=False)

    if all_done or (no_active and any_done and all_frames):
        return GroupStatusResult(status="done", should_persist=current_group_status != "done")

    if any_running:
        return GroupStatusResult(status="running", should_persist=current_group_status == "pending")

    if no_active and all_failed:
        return GroupStatusResult(status="failed", should_persist=current_group_status != "failed")

    return GroupStatusResult(status=current_group_status, should_persist=False)
