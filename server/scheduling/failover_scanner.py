"""
FailoverScanner — background daemon that detects stale/orphaned jobs and
triggers failover via the orchestrator.

Replaces the inline ``_check_failover`` that used to run inside the GET
handler.  Runs every ``interval_sec`` seconds (default 30) in a daemon thread.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from models.render_job import RenderJob
from models.value_objects import FAILOVER_STALE_SECONDS, is_serverless, now_iso
from infrastructure.db import execute, query_all, query_one

log = logging.getLogger(__name__)

SERVERLESS_PENDING_TIMEOUT_SEC = 300
SERVERLESS_MAX_RETRIES = 5


class FailoverScanner:

    def __init__(self, interval_sec: int = 10) -> None:
        self._interval = interval_sec
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="failover-scanner"
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
        groups = query_all(
            "SELECT * FROM render_groups WHERE status IN ('pending', 'running')"
        )
        for group in groups:
            try:
                jobs = query_all(
                    "SELECT * FROM jobs WHERE group_id = %s", (group["id"],)
                )
                self._scan_orphans(group, jobs)
                self._scan_stale_desktop(group, jobs)
                self._scan_serverless_retries(group, jobs)
            except Exception:
                log.exception("FailoverScanner error for group %s", group["id"])

        # Also clean up orphaned jobs in terminal groups
        terminal_groups = query_all(
            "SELECT * FROM render_groups WHERE status IN ('cancelled', 'failed')"
        )
        for group in terminal_groups:
            try:
                self._scan_terminal_group_orphans(group)
            except Exception:
                log.exception("FailoverScanner terminal-orphan error for group %s", group["id"])

    # ------------------------------------------------------------------
    # Scan: serverless orphans (stuck pending, completed-but-running)
    # ------------------------------------------------------------------

    def _scan_orphans(self, group: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        serverless_cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=SERVERLESS_PENDING_TIMEOUT_SEC)
        ).isoformat()

        for job in jobs:
            if job["status"] not in ("pending", "running"):
                continue
            machine = query_one("SELECT * FROM machines WHERE id = %s", (job["machine_id"],))
            if not machine or not is_serverless(machine.get("machine_type", "")):
                continue

            total = job.get("total_frames") or 0
            rendered = job.get("rendered_frames") or 0

            if job["status"] == "running" and total > 0 and rendered >= total:
                log.info(
                    f"Serverless job {job['id']} has {rendered}/{total} frames "
                    f"rendered but still 'running' — marking done"
                )
                execute(
                    "UPDATE jobs SET status = 'done', completed_at = %s, "
                    "rendered_frames = %s WHERE id = %s",
                    (now_iso(), total, job["id"]),
                )
                continue

            if job["status"] == "pending":
                submitted = job.get("submitted_at") or ""
                if not submitted or submitted >= serverless_cutoff:
                    continue
                log.warning(
                    f"Serverless job {job['id']} stuck in 'pending' since {submitted} — "
                    f"marking failed"
                )
                execute(
                    "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                    ("Dispatch timed out — no instance was created", now_iso(), job["id"]),
                )

    # ------------------------------------------------------------------
    # Scan: stale desktop machines
    # ------------------------------------------------------------------

    def _scan_stale_desktop(self, group: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        from scheduling.orchestrator import orchestrator

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=FAILOVER_STALE_SECONDS)
        ).isoformat()

        for job in jobs:
            if job["status"] != "running":
                continue
            machine = query_one("SELECT * FROM machines WHERE id = %s", (job["machine_id"],))
            if not machine:
                continue
            if is_serverless(machine.get("machine_type", "")):
                continue
            if (machine.get("last_seen_at") or "") >= cutoff:
                continue

            log.warning(f"Desktop job {job['id']} machine went offline — triggering failover")
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                ("Machine went offline", now_iso(), job["id"]),
            )

            rj = RenderJob.from_row({**job, "machine_type": machine.get("machine_type", "windows")})
            if rj.remaining_frames() is not None:
                orchestrator.handle_failure(
                    job=rj,
                    error="Machine went offline",
                    group_id=group["id"],
                )

    # ------------------------------------------------------------------
    # Scan: failed serverless retries
    # ------------------------------------------------------------------

    def _scan_serverless_retries(self, group: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        from scheduling.orchestrator import orchestrator

        covered_ranges: set[tuple[int, int]] = set()
        failed_range_counts: dict[tuple[int, int], int] = {}
        machines_cache: dict[str, dict[str, Any] | None] = {}

        for t in jobs:
            rng = (t["frame_start"], t["frame_end"])
            if t["status"] == "failed":
                failed_range_counts[rng] = failed_range_counts.get(rng, 0) + 1
            else:
                covered_ranges.add(rng)

        for job in jobs:
            if job["status"] != "failed":
                continue

            mid = job["machine_id"]
            if mid not in machines_cache:
                machines_cache[mid] = query_one("SELECT * FROM machines WHERE id = %s", (mid,))
            machine = machines_cache[mid]
            if not machine or not is_serverless(machine.get("machine_type", "")):
                continue

            rendered = max(0, job.get("rendered_frames") or 0)
            step = job.get("frame_step") or 1
            new_start = job["frame_start"] + rendered * step
            new_end = job["frame_end"]
            if new_start > new_end:
                continue

            if (new_start, new_end) in covered_ranges:
                continue

            rng = (job["frame_start"], job["frame_end"])
            if failed_range_counts.get(rng, 0) >= SERVERLESS_MAX_RETRIES:
                continue

            rj = RenderJob.from_row({**job, "machine_type": machine.get("machine_type", "")})
            result = orchestrator.handle_failure(
                job=rj,
                error=f"Retry after failure (attempt {rj.attempt})",
                group_id=group["id"],
            )
            if result:
                covered_ranges.add((new_start, new_end))
                log.info(
                    f"Created serverless retry for failed {job['id']} "
                    f"(frames {new_start}-{new_end})"
                )

    # ------------------------------------------------------------------
    # Scan: orphaned jobs in terminal groups
    # ------------------------------------------------------------------

    def _scan_terminal_group_orphans(self, group: dict[str, Any]) -> None:
        jobs = query_all(
            "SELECT * FROM jobs WHERE group_id = %s AND status IN ('pending', 'running')",
            (group["id"],),
        )
        if not jobs:
            return

        for j in jobs:
            rendered = j.get("rendered_frames") or 0
            total = j.get("total_frames") or 0
            if total > 0 and rendered >= total:
                execute(
                    "UPDATE jobs SET status = 'done', completed_at = %s, "
                    "rendered_frames = %s WHERE id = %s",
                    (now_iso(), total, j["id"]),
                )
                log.info(f"Orphaned job {j['id']} had all frames rendered — marked done")
            else:
                execute(
                    "UPDATE jobs SET status = %s, completed_at = %s, "
                    "error = %s WHERE id = %s",
                    (group["status"], now_iso(), f"Group was {group['status']}", j["id"]),
                )
                log.info(f"Orphaned job {j['id']} force-{group['status']}")


scanner = FailoverScanner()
