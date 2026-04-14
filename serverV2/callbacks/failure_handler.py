"""FailureHandler — marks a job failed and notifies the orchestrator to requeue.

Symmetric with SuccessHandler. Called ONLY from CallbackRouter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from serverV2.repositories.job_repository import JobRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class FailureHandler:

    def __init__(self, job_repo: JobRepository) -> None:
        self._job_repo = job_repo
        self._orchestrator: RenderOrchestrator | None = None

    def set_orchestrator(self, orchestrator: RenderOrchestrator) -> None:
        self._orchestrator = orchestrator

    def handle(self, job_id: str, error: str) -> None:
        self._job_repo.mark_failed(job_id, error)
        log.warning("Job %s marked failed: %s", job_id, error)
        if self._orchestrator:
            self._orchestrator.on_job_failed(job_id, error)
