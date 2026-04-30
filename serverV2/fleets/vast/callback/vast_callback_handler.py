"""VastCallbackHandler — thread manager for Vast instance monitors.

Spawns one VastInstanceMonitor thread per active job and tracks their
stop events so they can be cancelled individually or all at once.  The
tick narrative lives in VastInstanceMonitor.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

from serverV2.config import VastConfig
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.shared.job_counts import JobCounts
from serverV2.fleets.shared.liveness_check import LivenessCheck
from serverV2.fleets.shared.pre_render_stall_detector import IPreRenderStallDetector
from serverV2.fleets.vast.callback.vast_instance_monitor import VastInstanceMonitor
from serverV2.fleets.vast.callback.vast_snapshot_writer import VastSnapshotWriter
from serverV2.fleets.vast.callback.vast_status_classifier import VastStatusClassifier
from serverV2.fleets.vast.client import VastClient
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.progress_repository import ProgressRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class VastCallbackHandler:

    def __init__(
        self,
        config: VastConfig,
        client: VastClient,
        heartbeat_repo: HeartbeatRepository,
        progress_repo: ProgressRepository,
        on_failure: Callable[[str, str], None],
        on_success: Callable[[str], None],
        stall_detector_factory: Callable[[], IPreRenderStallDetector],
        registry: InstanceRegistry | None = None,
    ) -> None:
        self._cfg = config
        self._client = client
        self._heartbeats = heartbeat_repo
        self._progress = progress_repo
        self._on_failure = on_failure
        self._on_success = on_success
        self._stall_detector_factory = stall_detector_factory
        self._registry = registry
        self._monitors: dict[str, threading.Event] = {}
        self._monitors_lock = threading.Lock()
        self._orchestrator: "RenderOrchestrator | None" = None

    def set_orchestrator(self, orchestrator: "RenderOrchestrator") -> None:
        """Late-bound to break the orchestrator ↔ fleet construction cycle.
        Must be called before any ``start_monitoring`` invocation."""
        self._orchestrator = orchestrator

    def start_monitoring(
        self,
        *,
        job_id: str,
        provider_job_id: str,
        machine_id: str,
        blend_url: str,
        render_overrides_json: str,
        group_id: str,
    ) -> None:
        if self._orchestrator is None:
            raise RuntimeError(
                "VastCallbackHandler.set_orchestrator must be called before start_monitoring"
            )

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
        stall_detector = self._stall_detector_factory()
        monitor = VastInstanceMonitor(
            job_id=job_id,
            instance_id=instance_id,
            group_id=group_id,
            config=self._cfg,
            client=self._client,
            orchestrator=self._orchestrator,
            counts=counts,
            liveness=liveness,
            snapshot=snapshot,
            status=status,
            stall_detector=stall_detector,
            heartbeat_repo=self._heartbeats,
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
