"""VastFleetMonitor — singleton scanner for all active Vast jobs.

Replaces the per-job VastInstanceMonitor + VastMonitorManager pair.
One thread for the entire Vast fleet, gated by the ``monitor:vast``
Redis lock.  Each tick (~10s):

    1. SELECT active Vast rows from Postgres (one query)
    2. vast_client.list() — ONE Vast API call returning every instance
       on the account
    3. For each row: _inspect(row, fleet_dict[row.vast_job_id])

Memory: 1 thread regardless of active job count.  Replaces N threads
× ~10 MB each.

Per-job state that the per-job monitor used to hold on its instance
(LivenessCheck staleness counters, "did this container reach running
yet" timestamp) lives here in dicts keyed by job_id, persisted across
ticks and pruned when a row drops out of the active set.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from serverV2.config import VastConfig
from serverV2.config.vast.providers.vast_runtime_config_provider import (
    VastRuntimeConfigProvider,
)
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.shared.job_counts import JobCounts
from serverV2.fleets.shared.liveness_check import LivenessCheck
from serverV2.fleets.shared.pre_render_stall_detector import (
    HeartbeatWindow,
    IPreRenderStallDetector,
    StallReason,
)
from serverV2.fleets.vast.client import VastClient
from serverV2.fleets.vast.monitor.vast_snapshot_writer import VastSnapshotWriter
from serverV2.fleets.vast.monitor.vast_status_classifier import VastStatusClassifier
from serverV2.infrastructure.db import query_all
from serverV2.monitor_lock import MonitorLockRepository
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.repositories.progress_repository import ProgressRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SEC = 10
_MAX_CONSECUTIVE_API_ERRORS = 5


class _PerJobState:
    """State that used to live as instance fields on VastInstanceMonitor.
    One per active job, persisted across ticks of the singleton.
    """

    __slots__ = ("liveness", "became_running_at")

    def __init__(self, liveness: LivenessCheck) -> None:
        self.liveness = liveness
        self.became_running_at: float | None = None


class VastFleetMonitor:

    def __init__(
        self,
        *,
        config_provider: VastRuntimeConfigProvider,
        client: VastClient,
        group_repo: RenderGroupRepository,
        heartbeat_repo: HeartbeatRepository,
        progress_repo: ProgressRepository,
        output_frame_repo: OutputFrameRepository,
        on_failure: Callable[[str, str], None],
        on_success: Callable[[str], None],
        on_running: Callable[[str], None],
        update_spend_for_chunk: Callable[[dict], None],
        stall_detector_factory: Callable[[], IPreRenderStallDetector],
        lock_repo: MonitorLockRepository,
        instance_id: str,
        registry: InstanceRegistry | None = None,
        interval_sec: int = _DEFAULT_INTERVAL_SEC,
        status_repo=None,
    ) -> None:
        self._config_provider = config_provider
        self._client = client
        self._group_repo = group_repo
        self._heartbeats = heartbeat_repo
        self._progress = progress_repo
        self._output_frames = output_frame_repo
        self._on_failure = on_failure
        self._on_success = on_success
        self._on_running = on_running
        self._update_spend_for_chunk = update_spend_for_chunk
        self._stall_detector_factory = stall_detector_factory
        self._lock_repo = lock_repo
        self._instance_id = instance_id
        self._registry = registry
        self._interval = interval_sec

        self._counts = JobCounts(progress_repo, output_frame_repo)
        self._status = VastStatusClassifier()

        # Per-job state across ticks.
        self._per_job: dict[str, _PerJobState] = {}
        # Fleet-level API-error counter.  Replaces the per-job counter
        # that the per-job monitor kept.  Logged but does NOT fail jobs:
        # a Vast API blip should not declare 100 jobs failed; the next
        # tick will retry naturally.
        self._consecutive_api_errors = 0

        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

        # Heartbeat state for the admin dashboard's daemon-health panel.
        self._status_repo = status_repo
        self._tick_count = 0
        self._last_detail: dict = {}

    def _report_status(self, error: str | None = None) -> None:
        if self._status_repo is None:
            return
        self._status_repo.report(
            "vast_monitor", self._instance_id, self._tick_count,
            self._last_detail, error,
        )

    def try_start(self) -> bool:
        """Acquire the singleton ``monitor:vast`` lock and spawn the scan
        thread on win.  Returns True iff this instance now owns the
        monitor (or already did).  Idempotent within a process."""
        if not self._config_provider.get().is_enabled():
            return False
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            if not self._lock_repo.try_acquire(
                MonitorLockRepository.vast_key(),
                self._instance_id,
            ):
                return False
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="vast-fleet-monitor",
            )
            self._thread.start()
        log.info(
            "VastFleetMonitor started (interval=%ds)", self._interval,
        )
        return True

    def _loop(self) -> None:
        lock_key = MonitorLockRepository.vast_key()
        while True:
            if not self._lock_repo.refresh(lock_key, self._instance_id):
                log.info(
                    "VastFleetMonitor exiting -- lock taken by another instance",
                )
                with self._thread_lock:
                    self._thread = None
                return
            try:
                self._tick()
                self._tick_count += 1
                self._report_status()
            except Exception as exc:
                log.exception("VastFleetMonitor tick error")
                self._report_status(error=str(exc))
            time.sleep(self._interval)

    # ------------------------------------------------------------------
    # Per-tick scan
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        rows = query_all(
            """
            SELECT j.*,
                   rg.input_filename AS rg_input_filename,
                   COALESCE(rg.user_id, j.user_id) AS owner_uid
            FROM jobs j
            LEFT JOIN render_groups rg ON rg.id = j.group_id
            WHERE j.status IN ('running', 'pending')
              AND j.machine_type = 'vast_serverless'
              AND j.vast_job_id IS NOT NULL
            """,
        )
        try:
            fleet = self._client.instances.list()
            self._consecutive_api_errors = 0
        except Exception as exc:
            self._consecutive_api_errors += 1
            log.warning(
                "Vast list() failed (%d/%d): %s",
                self._consecutive_api_errors,
                _MAX_CONSECUTIVE_API_ERRORS, exc,
            )
            if self._consecutive_api_errors >= _MAX_CONSECUTIVE_API_ERRORS:
                log.error(
                    "Vast list() unreachable for %d consecutive ticks — "
                    "leaving jobs in flight; next tick retries",
                    self._consecutive_api_errors,
                )
            self._last_detail = {
                "jobs_scanned": len(rows),
                "instances_seen": None,
                "api_errors": self._consecutive_api_errors,
            }
            return

        fleet_dict: dict[int, dict[str, Any]] = {}
        for inst in fleet:
            try:
                fleet_dict[int(inst["id"])] = inst
            except (KeyError, ValueError, TypeError):
                continue

        self._last_detail = {
            "jobs_scanned": len(rows),
            "instances_seen": len(fleet_dict),
            "api_errors": self._consecutive_api_errors,
        }

        # One live config read per tick (assembled from Firestore knobs +
        # env secrets); threaded into _inspect for the legacy-row fallbacks.
        cfg = self._config_provider.get()

        active_job_ids: set[str] = set()
        for row in rows:
            job_id = row["id"]
            active_job_ids.add(job_id)
            # Continuous-billing tick: bring the user's recorded spend
            # for this chunk up to the current cost-so-far.  The row we
            # already fetched above has every field the billing path
            # needs (started_at, completed_at, price_per_hour, status,
            # owner_uid), so we pass it through directly -- no extra
            # repository call on the hot tick path.
            if str(row.get("status") or "") == "running":
                self._update_spend_for_chunk(row)
            try:
                provider_id = int(row["vast_job_id"])
            except (TypeError, ValueError):
                continue
            inst = fleet_dict.get(provider_id)
            try:
                self._inspect(row, inst, cfg)
            except Exception:
                log.exception("VastFleetMonitor inspect failed for %s", job_id)

        # Drop per-job state for rows that are no longer active.
        for stale_id in list(self._per_job.keys()):
            if stale_id not in active_job_ids:
                self._per_job.pop(stale_id, None)
                if self._registry is not None:
                    VastSnapshotWriter(
                        job_id=stale_id, registry=self._registry,
                    ).remove()

    # ------------------------------------------------------------------
    # Per-row inspection — ports VastInstanceMonitor._tick
    # ------------------------------------------------------------------

    def _inspect(
        self, row: dict[str, Any], inst: dict[str, Any] | None, cfg: VastConfig,
    ) -> None:
        job_id: str = row["id"]
        group_id: str = row.get("group_id") or ""
        local_status: str = str(row.get("status") or "")
        elapsed = self._elapsed_since_dispatch(row)

        # Resolved kill-time deadlines stamped at dispatch.  None for
        # legacy rows pre-dating the column -- rule evaluators silently
        # skip when their key is missing.
        deadlines = row.get("allowed_stall_times") or {}
        # Fallbacks to per-fleet config for pre-deploy rows.  After all
        # in-flight jobs settle these become dead branches we can prune.
        startup_timeout_sec = float(
            deadlines.get("startup_timeout_sec")
            if deadlines.get("startup_timeout_sec") is not None
            else cfg.startup_timeout_sec
        )
        heartbeat_grace_sec = float(
            deadlines.get("heartbeat_grace_sec")
            if deadlines.get("heartbeat_grace_sec") is not None
            else cfg.heartbeat_grace_sec
        )
        frame_progress_stale_sec = float(
            deadlines.get("frame_progress_stale_sec")
            if deadlines.get("frame_progress_stale_sec") is not None
            else cfg.in_progress_stale_sec
        )

        # Lazy per-job state.
        state = self._per_job.get(job_id)
        if state is None:
            state = _PerJobState(
                liveness=LivenessCheck(
                    heartbeat_repo=self._heartbeats,
                    defer_activation=True,
                ),
            )
            self._per_job[job_id] = state

        snapshot = VastSnapshotWriter(job_id=job_id, registry=self._registry)

        # Block: instance disappeared from Vast entirely.
        if inst is None:
            self._on_instance_gone(row)
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

        actual_status = self._status.normalize(str(inst.get("actual_status") or ""))
        status_msg = str(inst.get("status_msg") or "")

        # Block: fatal daemon-level error message.
        if self._status.has_fatal_error(actual_status, status_msg):
            self._on_fatal(row, inst)
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

        state.liveness.note_activity(self._counts.rendered(job_id, row))
        snapshot.write(row, actual_status, elapsed)

        # Block: DB says this job is already over.
        if local_status in ("done", "failed", "cancelled"):
            self._destroy(int(inst["id"]))
            if local_status == "failed":
                error = str(row.get("error") or "Worker reported failure")
                self._on_failure(job_id, error)
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

        # Block: group went terminal while this job was still active.
        if group_id and self._group_status(group_id) in ("done", "failed", "cancelled"):
            log.info(
                "Group %s is terminal — cleaning up Vast job %s",
                group_id, job_id,
            )
            self._destroy(int(inst["id"]))
            self._on_failure(job_id, "Group was terminal")
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

        # Block: success by verified uploads.
        if self._counts.is_complete(row) and local_status == "running":
            self._on_success(job_id)
            self._destroy(int(inst["id"]))
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

        # Block: startup timeout — container never reached running.
        if (state.became_running_at is None
                and not self._status.is_running(actual_status)
                and elapsed > startup_timeout_sec):
            err = f"Vast.ai instance stuck in '{actual_status}' for {elapsed:.0f}s"
            self._destroy(int(inst["id"]))
            self._on_failure(job_id, err)
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

        # Block: container is running.
        if self._status.is_running(actual_status):
            if state.became_running_at is None:
                state.became_running_at = time.monotonic()
                state.liveness.mark_active()
                if local_status == "pending":
                    self._on_running(job_id)
            if state.liveness.is_stale(stale_sec=frame_progress_stale_sec):
                err = (
                    f"Vast.ai job running but no new frames for "
                    f"{frame_progress_stale_sec / 60:.0f} min"
                )
                self._destroy(int(inst["id"]))
                self._on_failure(job_id, err)
                self._per_job.pop(job_id, None)
                snapshot.remove()
                return
            if state.liveness.heartbeat_dead(job_id, grace_sec=heartbeat_grace_sec):
                err = "Worker heartbeat stopped while Vast instance shows running"
                self._destroy(int(inst["id"]))
                self._on_failure(job_id, err)
                self._per_job.pop(job_id, None)
                snapshot.remove()
                return
            stall = self._evaluate_stall(row, elapsed, deadlines)
            if stall is not None:
                err = f"Pre-render stall ({stall.rule}): {stall.message}"
                log.warning("Job %s stalled: %s", job_id, err)
                self._destroy(int(inst["id"]))
                self._on_failure(job_id, err)
                self._per_job.pop(job_id, None)
                snapshot.remove()
                return

        # Block: container exited / stopped / offline.
        # Decision is purely data-driven now: complete = success, else
        # failure.  No 60-second wait-for-callback — under the sync
        # request model the worker doesn't exit until its terminal
        # callback returned 200, so by the time we see the exit the DB
        # is already in a terminal state OR no callback is ever coming.
        # The "DB terminal" block above handles the former; this block
        # handles the latter.
        if self._status.is_exited(actual_status):
            self._destroy(int(inst["id"]))
            if self._counts.is_complete(row):
                self._on_success(job_id)
            else:
                self._on_failure(
                    job_id, f"Vast.ai instance exited ({actual_status})",
                )
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _elapsed_since_dispatch(self, row: dict[str, Any]) -> float:
        """Seconds since the job row was created.  Replaces the per-job
        monitor's ``time.monotonic() - self._started_at`` since we no
        longer have a per-job construction moment."""
        from datetime import datetime, timezone
        ts = row.get("submitted_at")
        if not ts:
            return 0.0
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()

    def _group_status(self, group_id: str) -> str | None:
        grp = self._group_repo.get_by_id(group_id)
        if not grp:
            return None
        return str(grp.get("status") or "")

    def _evaluate_stall(
        self,
        row: dict[str, Any],
        elapsed: float,
        deadlines: dict[str, Any],
    ) -> StallReason | None:
        samples = self._heartbeats.get_recent(row["id"], n=60)
        if not samples:
            return None
        window = HeartbeatWindow.from_raw(samples)
        has_rendered = self._counts.uploaded(row) > 0
        return self._stall_detector_factory().evaluate(
            window,
            elapsed,
            has_rendered=has_rendered,
            allowed_stall_times=deadlines or None,
        )

    def _on_instance_gone(self, row: dict[str, Any]) -> None:
        job_id = row["id"]
        local_status = str(row.get("status") or "")
        if local_status in ("done", "failed", "cancelled"):
            return
        if self._counts.is_complete(row):
            self._on_success(job_id)
            return
        self._on_failure(job_id, "Vast.ai instance disappeared unexpectedly")

    def _on_fatal(self, row: dict[str, Any], inst: dict[str, Any]) -> None:
        err = (
            f"Vast.ai fatal startup error "
            f"(status={inst.get('actual_status')}): "
            f"{str(inst.get('status_msg', ''))[:200]}"
        )
        try:
            self._destroy(int(inst["id"]))
        except Exception:
            pass
        self._on_failure(row["id"], err)

    def _destroy(self, instance_id: int) -> None:
        try:
            self._client.instances.destroy(instance_id)
        except Exception as exc:
            log.warning("Vast destroy(%s) failed: %s", instance_id, exc)
