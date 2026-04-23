"""VastCallbackHandler — thread manager for Vast instance monitors.

Spawns one VastInstanceMonitor thread per active job and tracks their
stop events so they can be cancelled individually or all at once.  The
tick narrative lives in VastInstanceMonitor.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from serverV2.config import VastConfig
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.shared.job_counts import JobCounts
from serverV2.fleets.shared.liveness_check import LivenessCheck
from serverV2.fleets.vast.callback.vast_instance_monitor import VastInstanceMonitor
from serverV2.fleets.vast.callback.vast_snapshot_writer import VastSnapshotWriter
from serverV2.fleets.vast.callback.vast_status_classifier import VastStatusClassifier
from serverV2.fleets.vast.client import VastClient
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.progress_repository import ProgressRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


class VastCallbackHandler:

    def __init__(
        self,
        config: VastConfig,
        client: VastClient,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        heartbeat_repo: HeartbeatRepository,
        progress_repo: ProgressRepository,
        on_failure: Callable[[str, str], None],
        on_success: Callable[[str], None],
        registry: InstanceRegistry | None = None,
    ) -> None:
        self._cfg = config
        self._client = client
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._heartbeats = heartbeat_repo
        self._progress = progress_repo
        self._on_failure = on_failure
        self._on_success = on_success
        self._registry = registry
        self._monitors: dict[str, threading.Event] = {}
        self._monitors_lock = threading.Lock()

    def start_monitoring(
        self,
        *,
        job_id: str,
        provider_job_id: str,
        machine_id: str,
        blend_url: str,
        render_overrides_b64: str,
        group_id: str,
    ) -> None:
        stop_event = threading.Event()
        with self._monitors_lock:
            self._monitors[job_id] = stop_event

        instance_id = int(provider_job_id)
        counts = JobCounts(self._progress)
        liveness = LivenessCheck(
            heartbeat_repo=self._heartbeats,
            stale_sec=self._cfg.in_progress_stale_sec,
            heartbeat_grace_sec=self._cfg.heartbeat_grace_sec,
            defer_activation=True,
        )
        snapshot = VastSnapshotWriter(job_id=job_id, registry=self._registry)
        status = VastStatusClassifier()
        monitor = VastInstanceMonitor(
            job_id=job_id,
            instance_id=instance_id,
            group_id=group_id,
            config=self._cfg,
            client=self._client,
            job_repo=self._job_repo,
            group_repo=self._group_repo,
            counts=counts,
            liveness=liveness,
            snapshot=snapshot,
            status=status,
            on_failure=self._on_failure,
            on_success=self._on_success,
            stop_event=stop_event,
        )

        def _run_and_cleanup() -> None:
            try:
                monitor.run()
            finally:
                snapshot.remove()
                with self._monitors_lock:
                    self._monitors.pop(job_id, None)

        t = threading.Thread(
            target=_run_and_cleanup, daemon=True,
            name=f"vast-poll-{job_id[:8]}",
        )
        t.start()

    def stop_monitoring(self, job_id: str) -> None:
        """Signal a monitor to stop immediately."""
        with self._monitors_lock:
            event = self._monitors.get(job_id)
        if event:
            event.set()
            log.info("Signalled vast monitor for job %s to stop", job_id)

    def stop_all(self) -> None:
        """Signal all active monitors to stop."""
        with self._monitors_lock:
            for event in self._monitors.values():
                event.set()
            count = len(self._monitors)
        if count:
            log.info("Signalled %d vast monitors to stop", count)
