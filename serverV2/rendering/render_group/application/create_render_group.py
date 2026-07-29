"""Use-case: create a render group and hand back an input-upload URL.

The first step of a render.  Persists a new group in the ``uploading``
state and presigns the object-storage key the desktop app will push the
.blend to.  No allocation happens yet -- that waits for confirm-upload.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository
from serverV2.rendering.render_group.application.ports.storage_gateway import IStorageGateway
from serverV2.rendering.render_group.application.ports.user_service import IUserService


class CreateRenderGroup:
    def __init__(
        self,
        *,
        group_repo: IRenderGroupRepository,
        storage: IStorageGateway,
        users: IUserService,
    ) -> None:
        self._group_repo = group_repo
        self._storage = storage
        self._users = users

    def execute(self, *, user_id: str, input_filename: str) -> dict:
        raise NotImplementedError
