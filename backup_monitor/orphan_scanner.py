"""Per-tick scanner that finds heartbeat-dead jobs and reports them."""

from __future__ import annotations

import logging
from typing import Callable

import psycopg2.extensions
import redis

from heartbeat_checker import HeartbeatChecker
from orphan_reporter import OrphanReporter
from running_jobs_repository import RunningJobsRepository

log = logging.getLogger(__name__)


class OrphanScanner:
    """Owns one scan tick: fetch running jobs, check each heartbeat, report
    the dead ones.  Connections are built per tick via injected factories so
    the adapters stay stateless and we sidestep stale-TLS issues.
    """

    def __init__(
        self,
        jobs_repo: RunningJobsRepository,
        heartbeat_checker: HeartbeatChecker,
        orphan_reporter: OrphanReporter,
        db_factory: Callable[[], psycopg2.extensions.connection],
        redis_factory: Callable[[], redis.Redis],
    ) -> None:
        self._jobs_repo = jobs_repo
        self._heartbeat_checker = heartbeat_checker
        self._orphan_reporter = orphan_reporter
        self._db_factory = db_factory
        self._redis_factory = redis_factory

    def tick(self) -> None:
        db = self._db_factory()
        rds = self._redis_factory()
        try:
            rows = self._jobs_repo.find_running_serverless(db)
            log.info("Scan: %d running serverless job(s)", len(rows))
            for row in rows:
                job_id = row["id"]
                if self._heartbeat_checker.is_alive(rds, job_id):
                    continue
                log.info(
                    "Orphan detected: job %s (fleet=%s, group=%s) — heartbeat missing",
                    job_id, row.get("machine_type"), row.get("group_id"),
                )
                self._orphan_reporter.report(
                    job_id,
                    "Backup monitor: heartbeat lost, no active monitor",
                )
        finally:
            try:
                db.close()
            except Exception:
                pass
            try:
                rds.close()
            except Exception:
                pass
