"""CallbackRouter — single funnel for fleet-monitor outcome events.

Translates a fleet monitor's `(job_id, outcome, ...)` signal into the
matching ``RenderOrchestrator`` facade call:

    SUCCESS  → orchestrator.on_job_succeeded(job_id)
    FAILURE  → orchestrator.on_job_failed(job_id, error)

This layer holds zero state and touches no repositories.  It's the
adapter between fleet-specific monitor code and the orchestrator,
analogous to how ``api/routers/`` is the adapter between HTTP and the
orchestrator.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from serverV2.callbacks.failure_handler import FailureHandler
from serverV2.callbacks.success_handler import SuccessHandler
from serverV2.core.enums import CallbackOutcome

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class CallbackRouter:

    def __init__(
        self,
        orchestrator: "RenderOrchestrator",
        success_handler: SuccessHandler,
        failure_handler: FailureHandler,
    ) -> None:
        self._orchestrator = orchestrator
        self._success = success_handler
        self._failure = failure_handler

    def route(
        self,
        *,
        job_id: str,
        outcome: CallbackOutcome,
        error: str | None = None,
    ) -> None:
        # Skip already-terminal jobs to avoid double-processing of late
        # callbacks (e.g. a monitor tick fired between mark_done and the
        # monitor's own ``snapshot.remove()``).
        if self._orchestrator.is_job_terminal(job_id):
            log.info(
                "Job %s already terminal — ignoring %s callback",
                job_id, outcome.value,
            )
            return

        if outcome == CallbackOutcome.SUCCESS:
            # group_id is no longer needed by the handler — orchestrator
            # looks it up — but keep the signature stable for now.
            self._success.handle(job_id, "")
        elif outcome == CallbackOutcome.FAILURE:
            self._failure.handle(job_id, error or "Unknown failure")
