"""SuccessHandler — thin adapter from CallbackRouter to the orchestrator.

The full chunk-success flow (mark_done, in-progress release, telemetry,
fleet drain, group rollup) lives in ``RenderLifecycle.handle_chunk_succeeded``,
behind the ``RenderOrchestrator.on_job_succeeded`` facade method.

This handler exists as a layer so the CallbackRouter (which translates
fleet-monitor outcomes into orchestrator calls) doesn't import the
orchestrator directly.  Adapter pattern, kept symmetric with FailureHandler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator


class SuccessHandler:

    def __init__(self, orchestrator: "RenderOrchestrator") -> None:
        self._orchestrator = orchestrator

    def handle(self, job_id: str, group_id: str) -> None:
        # ``group_id`` accepted for symmetry with the router's signature;
        # the orchestrator looks it up itself from the job row.
        self._orchestrator.on_job_succeeded(job_id)
