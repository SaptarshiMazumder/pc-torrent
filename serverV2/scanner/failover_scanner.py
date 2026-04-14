"""FailoverScanner — background daemon that detects stale/orphaned jobs.

Uses repositories and orchestrator, not raw SQL.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from serverV2.core.enums import SERVERLESS_TYPE_VALUES
from serverV2.core.models import RenderJob
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)

_SERVERLESS_PENDING_TIMEOUT_SEC = 300
_SERVERLESS_MAX_RETRIES = 5


class FailoverScanner:

    def __init__(
        self,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        orchestrator: RenderOrchestrator,
        failover_stale_seconds: int = 30,
        interval_sec: int = 10,
    ) -> None:
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._orchestrator = orchestrator
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
                self._scan_serverless_retries(group, jobs)
            except Exception:
                log.exception("FailoverScanner error for group %s", group["id"])

        for group in self._group_repo.get_terminal_groups():
            try:
                self._scan_terminal_orphans(group)
            except Exception:
                log.exception("FailoverScanner terminal error for group %s", group["id"])

    # ---- scanning methods ----

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
                self._job_repo.mark_failed(job.job_id, "Dispatch timed out")

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

            self._job_repo.mark_failed(job.job_id, "Machine went offline")
            if job.remaining_frames() is not None:
                self._orchestrator.handle_failure(
                    job=job, error="Machine went offline", group_id=group["id"],
                )

    def _scan_serverless_retries(self, group: dict[str, Any], jobs: list[RenderJob]) -> None:
        covered: set[tuple[int, int]] = set()
        fail_counts: dict[tuple[int, int], int] = {}

        for j in jobs:
            rng = (j.frame_start, j.frame_end)
            if j.status == "failed":
                fail_counts[rng] = fail_counts.get(rng, 0) + 1
            else:
                covered.add(rng)

        for job in jobs:
            if job.status != "failed" or not job.is_serverless:
                continue
            remaining = job.remaining_frames()
            if remaining is None:
                continue
            new_start, new_end = remaining
            if (new_start, new_end) in covered:
                continue
            if fail_counts.get((job.frame_start, job.frame_end), 0) >= _SERVERLESS_MAX_RETRIES:
                continue

            result = self._orchestrator.handle_failure(
                job=job, error=f"Retry after failure (attempt {job.attempt})", group_id=group["id"],
            )
            if result:
                covered.add((new_start, new_end))

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
