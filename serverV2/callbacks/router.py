"""CallbackRouter — single gateway for all outcome notifications.

Fleet monitors and the failover scanner funnel through here.
Delegates to SuccessHandler and FailureHandler symmetrically.
"""

from __future__ import annotations

import logging

from serverV2.callbacks.failure_handler import FailureHandler
from serverV2.callbacks.success_handler import SuccessHandler
from serverV2.core.enums import CallbackOutcome
from serverV2.repositories.job_repository import JobRepository

log = logging.getLogger(__name__)


class CallbackRouter:

    def __init__(
        self,
        job_repo: JobRepository,
        success_handler: SuccessHandler,
        failure_handler: FailureHandler,
    ) -> None:
        self._job_repo = job_repo
        self._success = success_handler
        self._failure = failure_handler

    def route(
        self,
        *,
        job_id: str,
        outcome: CallbackOutcome,
        error: str | None = None,
        rendered_frames: int | None = None,
        total_frames: int | None = None,
        output_files: list[str] | None = None,
    ) -> None:
        job = self._job_repo.get_raw_by_id(job_id)
        if not job:
            log.warning("Callback for unknown job %s", job_id)
            return

        current = str(job.get("status") or "")
        if current in ("cancelled", "done"):
            log.info("Job %s already %s — ignoring %s callback", job_id, current, outcome.value)
            return

        group_id = job.get("group_id", "")

        if outcome == CallbackOutcome.SUCCESS:
            self._success.handle(job_id, group_id)

        elif outcome == CallbackOutcome.FAILURE:
            self._failure.handle(job_id, error or "Unknown failure")

        elif outcome == CallbackOutcome.PROGRESS:
            if rendered_frames is not None and total_frames is not None:
                self._job_repo.update_progress(job_id, rendered_frames, total_frames)
                if current == "pending":
                    self._job_repo.update_status(job_id, "running")
