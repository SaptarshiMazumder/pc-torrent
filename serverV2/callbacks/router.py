"""CallbackRouter — routes worker callbacks to the correct handler.

Workers report progress / success / failure via HTTP.  This router decides
which handler to invoke based on the outcome.
"""

from __future__ import annotations

import logging
from typing import Any

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

        group_id = job.get("group_id", "")

        if outcome == CallbackOutcome.SUCCESS:
            self._success.handle(job_id, group_id)

        elif outcome == CallbackOutcome.FAILURE:
            self._failure.handle(job, error or "Unknown failure", group_id)

        elif outcome == CallbackOutcome.PROGRESS:
            if rendered_frames is not None and total_frames is not None:
                self._job_repo.update_progress(job_id, rendered_frames, total_frames)
                if job["status"] == "pending":
                    self._job_repo.update_status(job_id, "running")

    def handle_failure_from_monitor(
        self, job: dict[str, Any], error: str, group_id: str,
    ) -> str | None:
        return self._failure.handle(job, error, group_id)
