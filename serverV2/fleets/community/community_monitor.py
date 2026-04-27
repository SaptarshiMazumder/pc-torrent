"""CommunityMonitor — leader-only scanner for community (desktop) jobs.

Community workers pull jobs from the DB instead of running inside a
container we dispatched to, so the per-job monitor pattern used by Modal
and Vast doesn't fit.  Instead, this daemon scans all active community
jobs every ``interval_sec`` and detects:

    1. Group went terminal while a job is still active — route FAILURE
       so the orchestrator marks the job to match.
    2. Machine went offline (last_seen_at stale) while job is running —
       route FAILURE so the orchestrator can requeue on another machine.

Both detections funnel through ``RenderOrchestrator.on_job_failed``.
This monitor still reads job/group/machine repositories directly because
its job is *discovery* — enumerating all candidate jobs — not single-job
state inspection like Vast/Modal monitors do.

Runs on the leader Cloud Run instance only.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from serverV2.core.models import RenderJob
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class CommunityMonitor:

    def __init__(
        self,
        *,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        machine_repo: MachineRepository,
        orchestrator: "RenderOrchestrator",
        stale_seconds: int = 30,
        interval_sec: int = 10,
    ) -> None:
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._machine_repo = machine_repo
        self._orchestrator = orchestrator
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
        # Orchestrator marks the job to match the group state
        # (handle_chunk_failed sees group terminal → skip retry → mark_failed).
        self._orchestrator.on_job_failed(
            job.job_id, f"Group was {group_status}",
        )

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
        self._orchestrator.on_job_failed(job.job_id, "Machine went offline")
