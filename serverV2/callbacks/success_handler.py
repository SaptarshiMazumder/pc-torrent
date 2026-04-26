"""SuccessHandler — marks a job done, drains the queue, asks the
orchestrator to roll the change up to the group.

Job-level mutations (mark_done, in-progress release, fleet drain) live
here.  Group-level state changes go through the orchestrator — this
handler never touches ``render_groups`` directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.dispatch.coordinator import DispatchCoordinator
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class SuccessHandler:

    def __init__(
        self,
        job_repo: JobRepository,
        in_progress_repo: InProgressChunkRepository,
    ) -> None:
        self._job_repo = job_repo
        self._in_progress = in_progress_repo
        self._orchestrator: RenderOrchestrator | None = None
        self._coordinator: DispatchCoordinator | None = None

    def set_orchestrator(self, orchestrator: "RenderOrchestrator") -> None:
        """Late-bound to break the circular wiring with the orchestrator
        (lifecycle composes the orchestrator + handlers, so the handlers
        cannot take it via __init__)."""
        self._orchestrator = orchestrator

    def set_coordinator(self, coordinator: "DispatchCoordinator") -> None:
        """Late-bound to break the circular wiring with the coordinator."""
        self._coordinator = coordinator

    def handle(self, job_id: str, group_id: str) -> None:
        # Release the chunk from the in-progress ledger FIRST so any late
        # failure signal for this job is recognized as stale and ignored.
        raw = self._job_repo.get_raw_by_id(job_id)
        fleet = (raw.get("machine_type") or "") if raw else ""
        if raw is not None:
            chunk_index = raw.get("chunk_index") or 0
            self._in_progress.release(group_id, chunk_index)

        self._job_repo.mark_done(job_id)
        log.info("Job %s marked done", job_id)

        if self._orchestrator is not None and group_id:
            self._orchestrator.on_job_succeeded(group_id)

        # A slot just opened up in this fleet — drain any waiting items.
        if fleet and self._coordinator is not None:
            try:
                self._coordinator.drain_for_fleet(fleet)
            except Exception as exc:
                log.warning("drain_for_fleet(%s) failed: %s", fleet, exc)
