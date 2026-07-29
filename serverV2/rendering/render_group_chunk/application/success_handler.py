"""Callback: react to a chunk reporting success.

Per-tick reaction: charge the user for the work this chunk did (FORWARD
cross-context call to billing via ``ICreditsService``), then funnel into
``ReconcileRenderGroupStatus`` -- the succeeded chunk may or may not make the
whole group terminal; that decision is the reconcile chokepoint's, not ours.

Composes the reconcile use-case rather than duplicating the rollup -- use-
cases calling use-cases is fine and keeps the chokepoint single.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.reconcile_render_group_status import (
    ReconcileRenderGroupStatus,
)
from serverV2.rendering.render_group.application.ports.credits_service import ICreditsService
from serverV2.rendering.render_group.application.ports.chunk_repository import IChunkRepository


class SuccessHandler:
    def __init__(
        self,
        *,
        chunk_repo: IChunkRepository,
        credits: ICreditsService,
        reconcile: ReconcileRenderGroupStatus,
    ) -> None:
        self._chunk_repo = chunk_repo
        self._credits = credits
        self._reconcile = reconcile

    def execute(self, job_id: str) -> None:
        raise NotImplementedError
