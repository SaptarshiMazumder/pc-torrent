"""Pure function: compute overall group status from per-job statuses
plus pending-allocation state.

A chunk parked on ``pending_allocation_queue`` (because the strategy
returned no eligible target at submit/retry time) has no row on
``jobs`` yet — but it IS still active work from the user's POV.  The
caller passes ``has_pending_allocation`` and we fold that in: pending
counts as active, so the all-failed / partial-failure branches don't
fire while parked work is waiting on the daemon's per-tick re-eval.
"""

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
    has_pending_allocation: bool = False,
) -> GroupStatusResult:
    # Fold pending-allocation state into "no_active": a parked row IS
    # active work, just not yet ledgered to the jobs table.  Every
    # branch that previously read no_active to mean "nothing left to
    # finish" now correctly waits for parked work too.
    no_active_jobs = not any(s in _ACTIVE for s in job_statuses)
    no_active = no_active_jobs and not has_pending_allocation
    all_frames = total_frames > 0 and total_rendered >= total_frames
    any_done = any(s == "done" for s in job_statuses)
    all_done = bool(job_statuses) and all(s == "done" for s in job_statuses)
    all_failed = bool(job_statuses) and all(s == "failed" for s in job_statuses)
    any_running = any(s == "running" for s in job_statuses)

    if current_group_status in _TERMINAL_GROUP:
        if not no_active:
            # Active retry jobs OR parked allocation work exist —
            # unlock from terminal state so the dispatch handler's
            # group-status guard lets the eventual promoted row through.
            return GroupStatusResult(status="running", should_persist=True)
        if no_active and any_done and all_frames:
            return GroupStatusResult(status="done", should_persist=current_group_status != "done")
        return GroupStatusResult(status=current_group_status, should_persist=False)

    if all_done or (no_active and any_done and all_frames):
        return GroupStatusResult(status="done", should_persist=current_group_status != "done")

    if any_running:
        return GroupStatusResult(status="running", should_persist=current_group_status == "pending")

    if no_active and all_failed:
        return GroupStatusResult(status="failed", should_persist=current_group_status != "failed")

    # No active jobs, not all frames rendered — partial failure.  Some chunks
    # succeeded, some exhausted retries, nothing more pending.  From the user's
    # POV the group didn't complete its job, so it's failed.
    if no_active and not all_frames:
        return GroupStatusResult(status="failed", should_persist=current_group_status != "failed")

    return GroupStatusResult(status=current_group_status, should_persist=False)
