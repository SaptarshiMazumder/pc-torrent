"""Use-case: confirm the .blend is uploaded and kick off rendering.

The heavy command.  Verifies the upload, reserves credits for the estimated
job (FORWARD cross-context call to billing), records the frame plan on the
group, then submits it to allocation to be planned and dispatched into
chunks.  The ``can_confirm_upload`` domain guard gates the transition.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.allocation_service import IAllocationService
from serverV2.rendering.render_group.application.ports.credits_service import ICreditsService
from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository
from serverV2.rendering.render_group.application.ports.storage_gateway import IStorageGateway


class ConfirmUpload:
    def __init__(
        self,
        *,
        group_repo: IRenderGroupRepository,
        storage: IStorageGateway,
        credits: ICreditsService,
        allocation: IAllocationService,
    ) -> None:
        self._group_repo = group_repo
        self._storage = storage
        self._credits = credits
        self._allocation = allocation

    def execute(self, group_id: str, *, frame_start: int, frame_end: int, frame_step: int) -> dict:
        raise NotImplementedError
