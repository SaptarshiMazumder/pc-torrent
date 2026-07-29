"""Use-case: delete a render group and its stored artifacts.

Removes the group row and purges its input + output objects from storage.
Ownership is checked against the requesting user before anything is touched.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository
from serverV2.rendering.render_group.application.ports.storage_gateway import IStorageGateway


class DeleteRenderGroup:
    def __init__(
        self,
        *,
        group_repo: IRenderGroupRepository,
        storage: IStorageGateway,
    ) -> None:
        self._group_repo = group_repo
        self._storage = storage

    def execute(self, group_id: str, *, user_id: str) -> dict:
        raise NotImplementedError
