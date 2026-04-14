"""FailoverScanner — background daemon that detects stale/orphaned jobs.

Pure failure detector.  Marks jobs failed and funnels through CallbackRouter
so the orchestrator can decide whether to requeue.  Zero retry logic here.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from serverV2.core.enums import CallbackOutcome, SERVERLESS_TYPE_VALUES
from serverV2.core.models import RenderJob
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

if TYPE_CHECKING:
    from serverV2.callbacks.router import CallbackRouter

log = logging.getLogger(__name__)

_SERVERLESS_PENDING_TIMEOUT_SEC = 300


class FailoverScanner:

    def __init__(
        self,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        callback_router: CallbackRouter,
        failover_stale_seconds: int = 30,
        interval_sec: int = 10,
    ) -> None:
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._router = callback_router
        self._failover_stale_sec = failover_stale_seconds
        self._interval = interval_sec
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="failover-scanner-v2",
        )
        self._thread.start()
        log.info("FailoverScanner started (interval=%ds)", self._interval)

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("FailoverScanner tick error")
            time.sleep(self._interval)

    def _tick(self) -> None:
        for group in self._group_repo.get_active_groups():
            try:
                jobs = self._job_repo.get_by_group(group["id"])
                self._scan_orphans(group, jobs)
                self._scan_stale_desktop(group, jobs)
            except Exception:
                log.exception("FailoverScanner error for group %s", group["id"])

        for group in self._group_repo.get_terminal_groups():
            try:
                self._scan_terminal_orphans(group)
            except Exception:
                log.exception("FailoverScanner terminal error for group %s", group["id"])

    def _scan_orphans(self, group: dict[str, Any], jobs: list[RenderJob]) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=_SERVERLESS_PENDING_TIMEOUT_SEC)
        ).isoformat()

        for job in jobs:
            if job.status not in ("pending", "running"):
                continue
            if not job.is_serverless:
                continue

            if job.status == "running" and job.is_complete_by_frames():
                self._job_repo.mark_done(job.job_id)
                continue

            if job.status == "pending" and job.submitted_at and job.submitted_at < cutoff:
                self._router.route(
                    job_id=job.job_id,
                    outcome=CallbackOutcome.FAILURE,
                    error="Dispatch timed out",
                )

    def _scan_stale_desktop(self, group: dict[str, Any], jobs: list[RenderJob]) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self._failover_stale_sec)
        ).isoformat()

        for job in jobs:
            if job.status != "running" or job.is_serverless:
                continue
            from serverV2.repositories.machine_repository import MachineRepository
            machine_repo = MachineRepository()
            machine = machine_repo.get_by_id(job.machine_id)
            if not machine:
                continue
            if (machine.last_seen_at or "") >= cutoff:
                continue

            if job.remaining_frames() is not None:
                self._router.route(
                    job_id=job.job_id,
                    outcome=CallbackOutcome.FAILURE,
                    error="Machine went offline",
                )

    def _scan_terminal_orphans(self, group: dict[str, Any]) -> None:
        active = self._job_repo.get_active_by_group(group["id"])
        for j in active:
            total = j.get("total_frames") or 0
            rendered = j.get("rendered_frames") or 0
            if total > 0 and rendered >= total:
                self._job_repo.mark_done(j["id"])
            else:
                self._job_repo.update_status(
                    j["id"], group["status"], error=f"Group was {group['status']}",
                )
