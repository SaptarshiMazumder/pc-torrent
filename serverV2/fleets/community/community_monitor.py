"""CommunityMonitor — leader-only scanner for community (desktop) jobs.

Community workers pull jobs from the DB instead of running inside a
container we dispatched to, so the per-job monitor pattern used by Modal
and Vast doesn't fit.  Instead, this daemon scans all active community
jobs every ``interval_sec`` and detects:

    1. Group went terminal while a job is still active — route FAILURE
       so the orchestrator marks the job to match.
    2. Machine went offline (last_seen_at stale) while job is running —
       route FAILURE so the orchestrator can requeue on another machine.
    3. Pre-render stall (CPU idle, byte stall, download/hard ceiling) —
       same shape as the Vast/Modal in-process detectors, evaluated
       here once per tick across all running community jobs.

Both detections funnel through the ``on_failure`` callback wired in
bootstrap, which routes via ``CallbackRouter`` — same path Vast/Modal
in-process monitors take.  This monitor still reads job/group/machine
repositories directly because its job is *discovery* — enumerating all
candidate jobs — not single-job state inspection like Vast/Modal
monitors do.

Runs on the leader Cloud Run instance only.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from serverV2.core.models import RenderJob
from serverV2.fleets.shared.pre_render_stall_detector import (
    HeartbeatWindow,
    IPreRenderStallDetector,
)
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


class CommunityMonitor:

    def __init__(
        self,
        *,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        machine_repo: MachineRepository,
        heartbeat_repo: HeartbeatRepository,
        on_failure: Callable[[str, str], None],
        stall_detector: IPreRenderStallDetector,
        stale_seconds: int = 30,
        interval_sec: int = 10,
    ) -> None:
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._machine_repo = machine_repo
        self._heartbeat_repo = heartbeat_repo
        self._on_failure = on_failure
        self._stall_detector = stall_detector
        self._stale_sec = stale_seconds
        self._interval = interval_sec
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="community-monitor",
        )
        self._thread.start()
        log.info("CommunityMonitor started (interval=%ds, stale=%ds)",
                 self._interval, self._stale_sec)

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("CommunityMonitor tick error")
            time.sleep(self._interval)

    def _tick(self) -> None:
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self._stale_sec)
        ).isoformat()

        for group in self._group_repo.get_active_groups():
            try:
                jobs = self._job_repo.get_by_group(group["id"])
                for job in jobs:
                    if job.is_serverless:
                        continue
                    self._check_group_terminal(group, job)
                    self._check_machine_offline(group, job, stale_cutoff)
                    self._check_pre_render_stall(job)
            except Exception:
                log.exception("CommunityMonitor error for group %s", group["id"])

    def _check_group_terminal(self, group: dict, job: RenderJob) -> None:
        if job.status in ("done", "failed", "cancelled"):
            return
        if group.get("status") not in ("done", "failed", "cancelled"):
            return
        group_status = group["status"]
        log.info(
            "Group %s is %s — reconciling community job %s",
            group["id"], group_status, job.job_id,
        )
        # CallbackRouter -> handle_chunk_failed sees group terminal
        # -> skip retry -> mark_failed.  Idempotency guard filters
        # double-fires (this scan + a parallel signal from elsewhere).
        self._on_failure(job.job_id, f"Group was {group_status}")

    def _check_machine_offline(
        self, group: dict, job: RenderJob, stale_cutoff: str,
    ) -> None:
        if job.status != "running":
            return
        machine = self._machine_repo.get_by_id(job.machine_id)
        if not machine:
            return
        if (machine.last_seen_at or "") >= stale_cutoff:
            return
        if job.remaining_frames() is None:
            # All frames already uploaded — let the success path finish.
            return
        self._on_failure(job.job_id, "Machine went offline")

    def _check_pre_render_stall(self, job: RenderJob) -> None:
        if job.status != "running":
            return
        samples = self._heartbeat_repo.get_recent(job.job_id, n=30)
        if not samples:
            return
        window = HeartbeatWindow.from_raw(samples)
        job_age = _seconds_since_iso(job.submitted_at)
        if job_age is None:
            return
        reason = self._stall_detector.evaluate(window, job_age)
        if reason is None:
            return
        log.warning(
            "Community job %s stalled (%s): %s",
            job.job_id, reason.rule, reason.message,
        )
        self._on_failure(
            job.job_id,
            f"Pre-render stall ({reason.rule}): {reason.message}",
        )


def _seconds_since_iso(iso_ts: str) -> float | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()
