"""CommunityMonitor — singleton scanner for community (desktop) jobs.

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

Singleton across the fleet of Cloud Run instances: at most one runs at
a time, coordinated via the ``monitor:community`` Redis lock.  ``try_start``
is the only entry point — it acquires the lock and spawns the thread
on win, returns False on lose.  The loop refreshes the lock every tick
and self-terminates on lost ownership so a sweeper on another instance
can take over after a crash.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from serverV2.core.models import RenderJob
from serverV2.fleets.shared.job_counts import JobCounts
from serverV2.fleets.shared.pre_render_stall_detector import (
    HeartbeatWindow,
    IPreRenderStallDetector,
)
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.monitor_lock import MonitorLockRepository
from serverV2.services.machines.machine_redis_mirror import MachineRedisMirror
from serverV2.services.machines.machine_repository import MachineRepository
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.repositories.progress_repository import ProgressRepository
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
        machine_redis_mirror: MachineRedisMirror,
        output_frame_repo: OutputFrameRepository,
        progress_repo: ProgressRepository,
        on_failure: Callable[[str, str], None],
        stall_detector: IPreRenderStallDetector,
        lock_repo: MonitorLockRepository,
        instance_id: str,
        dispatch_claim_timeout_sec: int,
        demote_seconds: int = 90,
        interval_sec: int = 10,
    ) -> None:
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._machine_repo = machine_repo
        self._heartbeat_repo = heartbeat_repo
        self._mirror = machine_redis_mirror
        self._output_frames = output_frame_repo
        # Same JobCounts shape Modal and Vast monitors construct -- single
        # source of truth for "is this job complete?" semantics so all
        # three fleets agree on what "done" means.
        self._counts = JobCounts(progress_repo, output_frame_repo)
        self._on_failure = on_failure
        self._stall_detector = stall_detector
        self._lock_repo = lock_repo
        self._instance_id = instance_id
        # ``demote_seconds`` (~90s): how long without an idle-phase
        # heartbeat (machines:alive ZADD) before we flip a status=
        # 'available' machine to 'idle'.  Wider than the agent's poll
        # interval so a normally-polling agent can't be demoted in a
        # race window.  Render-phase liveness goes through the per-job
        # heartbeat directly, not this threshold.
        self._demote_sec = demote_seconds
        # Mirrors Modal/Vast: a community job stuck in status='pending'
        # past this threshold means the agent never claimed it (most
        # likely crashed between dispatch and next poll).  Failing it
        # routes through the standard CallbackRouter -> retry pipeline.
        self._dispatch_claim_timeout = dispatch_claim_timeout_sec
        self._interval = interval_sec
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def try_start(self) -> bool:
        """Acquire the singleton ``monitor:community`` lock and spawn the
        scan thread on win.  Returns True iff this instance now owns the
        monitor.  Idempotent within a single process: a second call
        while we already own the lock is a no-op (returns True).

        Called both at boot (every Cloud Run instance attempts; only the
        first wins) and from the MonitorLockSweeper tick (when the
        previous owner died and the lock TTL expired)."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            if not self._lock_repo.try_acquire(
                MonitorLockRepository.community_key(),
                self._instance_id,
            ):
                return False
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="community-monitor",
            )
            self._thread.start()
        log.info(
            "CommunityMonitor started (interval=%ds, demote=%ds)",
            self._interval, self._demote_sec,
        )
        return True

    def _loop(self) -> None:
        lock_key = MonitorLockRepository.community_key()
        while True:
            # Compare-and-extend the singleton lock.  False = our TTL
            # expired (Redis blip / GC pause) and another instance's
            # sweeper has taken over -- exit so we don't double-write.
            if not self._lock_repo.refresh(lock_key, self._instance_id):
                log.info(
                    "CommunityMonitor exiting -- lock taken by another instance",
                )
                with self._thread_lock:
                    self._thread = None
                return
            try:
                self._tick()
            except Exception:
                log.exception("CommunityMonitor tick error")
            time.sleep(self._interval)

    def _tick(self) -> None:
        # Single Redis read for the whole tick.  Used as a fallback
        # liveness signal in _check_machine_offline AND for the
        # demote-ghosts pass at the bottom -- no reason to query it
        # twice per tick.
        machine_alive_ids = self._mirror.alive_ids(self._demote_sec)

        for group in self._group_repo.get_active_groups():
            try:
                jobs = self._job_repo.get_by_group(group["id"])
                for job in jobs:
                    if job.is_serverless:
                        continue
                    self._check_group_terminal(group, job)
                    self._check_machine_offline(job, machine_alive_ids)
                    self._check_pre_render_stall(job)
            except Exception:
                log.exception("CommunityMonitor error for group %s", group["id"])

        # Cross-group sweep: pending community rows whose agent never
        # claimed the dispatch.  One query for the whole fleet rather
        # than re-checking every active group's job list.
        try:
            self._check_pending_dispatch_timeout()
        except Exception:
            log.exception("CommunityMonitor pending-dispatch sweep failed")

        # Demote ghost machines: status='available' in Postgres but
        # absent from the WIDE liveness window (90s).  Only 'available'
        # -- 'processing' machines are exempt because their liveness
        # flows through the active job's heartbeat, not machines:alive.
        # Skip if Redis is down (mass-demote risk).  Prune stale entries
        # from the sorted set so it doesn't grow unboundedly.
        if machine_alive_ids is not None:
            try:
                demoted = self._machine_repo.demote_ghosts(machine_alive_ids)
                if demoted:
                    log.info("Demoted %d ghost machine(s) to idle", demoted)
                self._mirror.prune_stale(self._demote_sec)
            except Exception:
                log.exception("Ghost demote / prune failed")

    def _check_pending_dispatch_timeout(self) -> None:
        """Fail community jobs stuck in 'pending' past the claim timeout.
        Mirrors ``VastFleetMonitor`` startup-timeout and
        ``ModalFleetMonitor`` in-queue-timeout: dispatch happened, agent
        never claimed.  Failure flows through CallbackRouter ->
        release_machine_step (flips machine back to 'available') ->
        retry pipeline.
        """
        rows = self._job_repo.get_stale_pending_community(
            self._dispatch_claim_timeout,
        )
        for row in rows:
            log.warning(
                "Community job %s stuck pending > %ds on machine %s -- failing",
                row["id"], self._dispatch_claim_timeout, row.get("machine_id"),
            )
            self._on_failure(
                row["id"],
                f"Agent did not claim within {self._dispatch_claim_timeout}s",
            )

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
        self,
        job: RenderJob,
        machine_alive_ids: set[str] | None,
    ) -> None:
        """Fire 'Machine went offline' iff the agent has stopped pinging.

        Two liveness signals are consulted -- either being fresh proves
        the agent is alive:

        1. ``job:{id}:hb`` (TTL key, written every 5s by HeartbeatSender)
           -- the render-phase per-job heartbeat.
        2. ``machines:alive`` (sorted set, ZADD on every
           /jobs/next-for-machine poll) -- the idle-phase heartbeat,
           kept warm by polling.

        Why both: between job claim (the poll's response) and the
        agent's first /jobs/{id}/heartbeat call (~1s later), only
        signal #2 exists.  Falsely declaring offline in that window
        would kill every freshly-claimed community job whose monitor
        tick happened to land inside the gap.  After ~90s of rendering
        without polling, signal #2 ages out and #1 takes over as the
        sole liveness check -- which is correct for long renders.
        """
        if job.status != "running":
            return
        alive = self._heartbeat_repo.is_alive(job.job_id)
        if alive is None:
            # Redis unavailable -- can't reliably detect.  backup_monitor
            # (cron, every 60s) catches absolute orphans independently.
            return
        if alive:
            return
        # Per-job heartbeat is missing.  Fall back to machines:alive --
        # the agent may be in the post-claim window, hasn't sent the
        # first per-job heartbeat yet, but its last poll's ZADD is
        # still fresh in the sorted set.
        if machine_alive_ids is not None and job.machine_id in machine_alive_ids:
            return
        # Both signals say dead.  Suppress if all frames already uploaded
        # so the success path can finish naturally.  Delegates to the
        # shared JobCounts facade Modal and Vast monitors also use, so
        # community treats "complete" with the same semantics.
        if self._counts.is_complete(
            {"id": job.job_id, "total_frames": job.total_frames},
        ):
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
        # ``has_rendered`` and ``estimated_startup_sec`` feed LoadingStallRule.
        # Community monitor scans all jobs in one thread, so we read both
        # per-job rather than holding state on the monitor.  ``uploaded``
        # only needs the row's id.
        has_rendered = self._counts.uploaded({"id": job.job_id}) > 0
        reason = self._stall_detector.evaluate(
            window,
            job_age,
            has_rendered=has_rendered,
            estimated_startup_sec=float(job.estimated_startup_seconds or 0.0),
        )
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
