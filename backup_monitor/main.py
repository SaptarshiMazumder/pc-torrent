"""Backup monitor entry point — single scan tick, then exit.

Runs as a Cloud Run Job triggered every 60s by Cloud Scheduler.

Why this exists:
  * Per-job monitor threads live inside the main serverV2 process.
  * If that process dies (Cloud Run restart, OOM, deploy), the threads
    die with it.  Boot recovery should reattach them but only fires on
    leader restart, leaving a window.
  * This job closes that window — it's an external observer with one
    job: notice heartbeat-dead jobs and tell the orchestrator.

What this is NOT:
  * Not a per-job monitor — it does no polling of Vast/Modal.
  * Not a state authority — it just reads, never writes Postgres.
  * Not a replacement for the in-process monitors — they handle the
    fast (15s) detection path during normal operation.
"""

from __future__ import annotations

import logging
import sys

import psycopg2
import redis

from backup_monitor_config import BackupMonitorConfig
from heartbeat_checker import HeartbeatChecker
from orphan_reporter import OrphanReporter
from orphan_scanner import OrphanScanner
from running_jobs_repository import RunningJobsRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backup_monitor")


def _build_scanner(cfg: BackupMonitorConfig) -> OrphanScanner:
    return OrphanScanner(
        jobs_repo=RunningJobsRepository(),
        heartbeat_checker=HeartbeatChecker(),
        orphan_reporter=OrphanReporter(
            orchestrator_url=cfg.orchestrator_url,
            orphan_secret=cfg.orphan_secret,
            http_timeout_sec=cfg.http_timeout_sec,
        ),
        db_factory=lambda: psycopg2.connect(
            cfg.database_url,
            keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=3,
        ),
        redis_factory=lambda: redis.from_url(cfg.redis_url, socket_timeout=5),
    )


def main() -> int:
    cfg = BackupMonitorConfig.from_env()
    log.info("Backup monitor tick starting (orchestrator=%s)", cfg.orchestrator_url)
    scanner = _build_scanner(cfg)
    scanner.tick()
    log.info("Backup monitor tick complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
