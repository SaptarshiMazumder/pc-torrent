"""FailureHandler — thin adapter from CallbackRouter to the orchestrator.

The full chunk-failure flow (retry decision, mark_failed, fleet drain,
group rollup) lives in ``RenderLifecycle.handle_chunk_failed``, behind
the ``RenderOrchestrator.on_job_failed`` facade method.

This handler exists as a layer so the CallbackRouter (which translates
fleet-monitor outcomes into orchestrator calls) doesn't import the
orchestrator directly.  Adapter pattern, kept symmetric with SuccessHandler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator


class FailureHandler:

    def __init__(self, orchestrator: "RenderOrchestrator") -> None:
        self._orchestrator = orchestrator

    def handle(self, job_id: str, error: str) -> None:
        self._orchestrator.on_job_failed(job_id, error)
