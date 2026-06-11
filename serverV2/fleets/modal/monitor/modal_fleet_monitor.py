"""ModalFleetMonitor — singleton scanner for all active Modal jobs.

Replaces the per-job ModalJobMonitor + ModalMonitorManager pair.
One thread for the entire Modal fleet, gated by the ``monitor:modal``
Redis lock.  Each tick (~10s):

    1. SELECT active Modal rows from Postgres (one query)
    2. For each row: _inspect(row)

Modal differs from Vast in one important way: the per-job tick never
queried Modal's API for state — it only read Postgres + Redis.  So
this singleton has no equivalent of Vast's bulk ``list()`` call.  The
only Modal SDK touch is ``cancel_job`` on terminal events, which fires
once per row when needed.

Per-job state (LivenessCheck staleness counters) lives in a dict keyed
by job_id, persisted across ticks and pruned when a row drops out.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from serverV2.config import ModalConfig
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.modal.client import ModalClient
from serverV2.fleets.modal.monitor.modal_snapshot_writer import ModalSnapshotWriter
from serverV2.fleets.shared.job_counts import JobCounts
from serverV2.fleets.shared.liveness_check import LivenessCheck
from serverV2.fleets.shared.pre_render_stall_detector import (
    HeartbeatWindow,
    IPreRenderStallDetector,
    StallReason,
)
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
_HEARTBEAT_GRACE_SEC = 90


class _PerJobState:
    __slots__ = ("liveness",)

    def __init__(self, liveness: LivenessCheck) -> None:
        self.liveness = liveness


class ModalFleetMonitor:

    def __init__(
        self,
        *,
        config: ModalConfig,
        client: ModalClient,
        group_repo: RenderGroupRepository,
        heartbeat_repo: HeartbeatRepository,
        progress_repo: ProgressRepository,
        output_frame_repo: OutputFrameRepository,
        on_failure: Callable[[str, str], None],
        on_success: Callable[[str], None],
        update_spend_for_chunk: Callable[[dict], None],
        stall_detector_factory: Callable[[], IPreRenderStallDetector],
        lock_repo: MonitorLockRepository,
        instance_id: str,
        registry: InstanceRegistry | None = None,
        interval_sec: int = _DEFAULT_INTERVAL_SEC,
    ) -> None:
        self._cfg = config
        self._client = client
        self._group_repo = group_repo
        self._heartbeats = heartbeat_repo
        self._progress = progress_repo
        self._output_frames = output_frame_repo
        self._on_failure = on_failure
        self._on_success = on_success
        self._update_spend_for_chunk = update_spend_for_chunk
        self._stall_detector_factory = stall_detector_factory
        self._lock_repo = lock_repo
        self._instance_id = instance_id
        self._registry = registry
        self._interval = interval_sec

        self._counts = JobCounts(progress_repo, output_frame_repo)

        self._per_job: dict[str, _PerJobState] = {}

        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def try_start(self) -> bool:
        if not self._cfg.is_enabled():
            return False
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            if not self._lock_repo.try_acquire(
                MonitorLockRepository.modal_key(),
                self._instance_id,
            ):
                return False
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="modal-fleet-monitor",
            )
            self._thread.start()
        log.info(
            "ModalFleetMonitor started (interval=%ds)", self._interval,
        )
        return True

    def _loop(self) -> None:
        lock_key = MonitorLockRepository.modal_key()
        while True:
            if not self._lock_repo.refresh(lock_key, self._instance_id):
                log.info(
                    "ModalFleetMonitor exiting -- lock taken by another instance",
                )
                with self._thread_lock:
                    self._thread = None
                return
            try:
                self._tick()
            except Exception:
                log.exception("ModalFleetMonitor tick error")
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
              AND j.machine_type = 'modal_serverless'
              AND j.modal_function_call_id IS NOT NULL
            """,
        )

        active_job_ids: set[str] = set()
        for row in rows:
            job_id = row["id"]
            active_job_ids.add(job_id)
            if str(row.get("status") or "") == "running":
                # Continuous-billing tick: row already has owner_uid +
                # cost fields from the bulk SELECT above; pass through.
                # See VastFleetMonitor._tick for the full narrative.
                self._update_spend_for_chunk(row)
            try:
                self._inspect(row)
            except Exception:
                log.exception("ModalFleetMonitor inspect failed for %s", job_id)

        # Drop per-job state for rows that are no longer active.
        for stale_id in list(self._per_job.keys()):
            if stale_id not in active_job_ids:
                self._per_job.pop(stale_id, None)
                if self._registry is not None:
                    ModalSnapshotWriter(
                        job_id=stale_id, registry=self._registry,
                    ).remove()

    # ------------------------------------------------------------------
    # Per-row inspection — ports ModalJobMonitor._tick
    # ------------------------------------------------------------------

    def _inspect(self, row: dict[str, Any]) -> None:
        job_id: str = row["id"]
        provider_job_id: str = str(row.get("modal_function_call_id") or "")
        if not provider_job_id:
            return

        group_id: str = row.get("group_id") or ""
        local_status: str = str(row.get("status") or "")
        elapsed = self._elapsed_since_dispatch(row)

        # Resolved kill-time deadlines stamped at dispatch.  None for
        # legacy rows -- fall back to per-fleet config below.
        deadlines = row.get("allowed_stall_times") or {}
        in_queue_timeout_sec = float(
            deadlines.get("in_queue_timeout_sec")
            if deadlines.get("in_queue_timeout_sec") is not None
            else self._cfg.in_queue_timeout_sec
        )
        heartbeat_grace_sec = float(
            deadlines.get("heartbeat_grace_sec")
            if deadlines.get("heartbeat_grace_sec") is not None
            else _HEARTBEAT_GRACE_SEC
        )
        frame_progress_stale_sec = float(
            deadlines.get("frame_progress_stale_sec")
            if deadlines.get("frame_progress_stale_sec") is not None
            else self._cfg.in_progress_stale_sec
        )

        state = self._per_job.get(job_id)
        if state is None:
            state = _PerJobState(
                liveness=LivenessCheck(heartbeat_repo=self._heartbeats),
            )
            self._per_job[job_id] = state

        snapshot = ModalSnapshotWriter(job_id=job_id, registry=self._registry)
        state.liveness.note_activity(self._counts.rendered(job_id, row))
        snapshot.write(row, elapsed)

        # Block: DB says this job is already over.
        if local_status in ("done", "failed", "cancelled"):
            self._cancel(provider_job_id)
            if local_status == "failed":
                error = str(row.get("error") or "Worker reported failure")
                self._on_failure(job_id, error)
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

        # Block: group went terminal while this job was still active.
        if group_id and self._group_status(group_id) in ("done", "failed", "cancelled"):
            log.info(
                "Group %s is terminal — cleaning up Modal job %s",
                group_id, job_id,
            )
            self._cancel(provider_job_id)
            self._on_failure(job_id, "Group was terminal")
            self._per_job.pop(job_id, None)
            snapshot.remove()
            return

        # Block: success by verified uploads.
        if self._counts.is_complete(row):
            self._handle_success(job_id, provider_job_id, snapshot)
            return

        # Block: dispatch lost — pending past queue window AND worker silent.
        if local_status == "pending" and elapsed > in_queue_timeout_sec:
            if state.liveness.heartbeat_dead(job_id, grace_sec=heartbeat_grace_sec):
                self._handle_failure(
                    job_id, provider_job_id, snapshot,
                    f"Modal dispatch lost — pending for {elapsed:.0f}s with no heartbeat",
                )
                return

        # Block: heartbeat dead while running.
        if local_status == "running" and state.liveness.heartbeat_dead(
            job_id, grace_sec=heartbeat_grace_sec,
        ):
            self._handle_failure(
                job_id, provider_job_id, snapshot,
                "Modal job heartbeat dead",
            )
            return

        # Block: frame-progress staleness (only fires after first frame uploads).
        if local_status == "running" and state.liveness.is_stale(
            stale_sec=frame_progress_stale_sec,
        ):
            self._handle_failure(
                job_id, provider_job_id, snapshot,
                f"Modal job stale — no new frames for {frame_progress_stale_sec / 60:.0f} min",
            )
            return

        # Block: pre-render stall.
        if local_status == "running":
            stall = self._evaluate_stall(row, elapsed, deadlines)
            if stall is not None:
                self._handle_failure(
                    job_id, provider_job_id, snapshot,
                    f"Pre-render stall ({stall.rule}): {stall.message}",
                )
                return

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _elapsed_since_dispatch(self, row: dict[str, Any]) -> float:
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
        samples = self._heartbeats.get_recent(row["id"], n=30)
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

    def _handle_success(
        self,
        job_id: str,
        provider_job_id: str,
        snapshot: ModalSnapshotWriter,
    ) -> None:
        self._cancel(provider_job_id)
        self._on_success(job_id)
        self._per_job.pop(job_id, None)
        snapshot.remove()

    def _handle_failure(
        self,
        job_id: str,
        provider_job_id: str,
        snapshot: ModalSnapshotWriter,
        error: str,
    ) -> None:
        log.warning("Job %s: %s", job_id, error)
        self._cancel(provider_job_id)
        self._on_failure(job_id, error)
        self._per_job.pop(job_id, None)
        snapshot.remove()

    def _cancel(self, provider_job_id: str) -> None:
        try:
            self._client.cancel_job(provider_job_id)
        except Exception as exc:
            log.warning("Modal cancel(%s) failed: %s", provider_job_id, exc)
