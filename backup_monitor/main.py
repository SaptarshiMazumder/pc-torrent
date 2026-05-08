"""Backup monitor entry point — single scan tick, then exit.

Runs as a Cloud Run Job triggered every 60s by Cloud Scheduler.

Why this exists:
  * Per-job monitor threads live inside the main serverV2 process.
  * If that process dies (Cloud Run restart, OOM, deploy), the threads
    die with it.  Boot recovery should reattach them but only fires on
    leader restart, leaving a window.
  * This job closes that window — it's an external observer with three
    jobs:
      1. notice heartbeat-dead jobs and tell the orchestrator;
      2. notice provider-side Vast instances that are ghosts (terminal
         locally, or have no local row at all and are old enough that
         it's not a fresh dispatch) and tell the orchestrator;
      3. ask the orchestrator to (re-)cancel any locally-terminal Modal
         function calls we still hold ids for.

What this is NOT:
  * Not a per-job monitor — it does no polling of Vast/Modal during
    normal operation.
  * Not a state authority — it just reads, never writes Postgres.  All
    destructive actions are delegated to the orchestrator over HTTP
    using the shared ``X-Orphan-Secret`` header.
  * Not a replacement for the in-process monitors — they handle the
    fast (15s) detection path during normal operation.
"""

from __future__ import annotations

import logging
import sys

import psycopg2
import redis

from backup_monitor_config import BackupMonitorConfig
from ghost_reporter import GhostReporter
from heartbeat_checker import HeartbeatChecker
from modal_terminal_jobs_repository import ModalTerminalJobsRepository
from modal_terminal_scanner import ModalTerminalScanner
from orphan_reporter import OrphanReporter
from orphan_scanner import OrphanScanner
from running_jobs_repository import RunningJobsRepository
from vast_api_client import VastApiClient
from vast_ghost_scanner import VastGhostScanner
from vast_jobs_repository import VastJobsRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backup_monitor")


def _db_factory(cfg: BackupMonitorConfig):
    return lambda: psycopg2.connect(
        cfg.database_url,
        keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=3,
    )


def _redis_factory(cfg: BackupMonitorConfig):
    return lambda: redis.from_url(cfg.redis_url, socket_timeout=5)


def _build_orphan_scanner(cfg: BackupMonitorConfig) -> OrphanScanner:
    return OrphanScanner(
        jobs_repo=RunningJobsRepository(),
        heartbeat_checker=HeartbeatChecker(),
        orphan_reporter=OrphanReporter(
            orchestrator_url=cfg.orchestrator_url,
            orphan_secret=cfg.orphan_secret,
            http_timeout_sec=cfg.http_timeout_sec,
        ),
        db_factory=_db_factory(cfg),
        redis_factory=_redis_factory(cfg),
    )


def _build_vast_ghost_scanner(
    cfg: BackupMonitorConfig, reporter: GhostReporter,
) -> VastGhostScanner:
    return VastGhostScanner(
        vast_api=VastApiClient(
            api_key=cfg.vast_api_key,
            http_timeout_sec=cfg.http_timeout_sec,
        ),
        vast_jobs_repo=VastJobsRepository(),
        reporter=reporter,
        min_instance_age_sec=cfg.vast_ghost_min_age_sec,
        db_factory=_db_factory(cfg),
    )


def _build_modal_terminal_scanner(
    cfg: BackupMonitorConfig, reporter: GhostReporter,
) -> ModalTerminalScanner:
    return ModalTerminalScanner(
        modal_terminal_jobs_repo=ModalTerminalJobsRepository(),
        reporter=reporter,
        window_hours=cfg.modal_terminal_window_hours,
        max_rows=cfg.modal_terminal_max_rows,
        db_factory=_db_factory(cfg),
    )


def main() -> int:
    cfg = BackupMonitorConfig.from_env()
    log.info("Backup monitor tick starting (orchestrator=%s)", cfg.orchestrator_url)

    ghost_reporter = GhostReporter(
        orchestrator_url=cfg.orchestrator_url,
        orphan_secret=cfg.orphan_secret,
        http_timeout_sec=cfg.http_timeout_sec,
    )

    # Heartbeat orphans (existing).
    _build_orphan_scanner(cfg).tick()

    # Vast provider-side ghosts.  Each tick is independent of the
    # others -- a failure in one shouldn't cancel the others.
    try:
        _build_vast_ghost_scanner(cfg, ghost_reporter).tick()
    except Exception:
        log.exception("Vast ghost scan tick failed")

    # Modal terminal-cleanup retry.
    try:
        _build_modal_terminal_scanner(cfg, ghost_reporter).tick()
    except Exception:
        log.exception("Modal terminal-cleanup tick failed")

    log.info("Backup monitor tick complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
