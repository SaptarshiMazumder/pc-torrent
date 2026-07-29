"""Retrieval: the rendered output frames of a group.

Lists the group's output objects and presigns each for download.  The
zip-streaming variant lives with the same port set; this use-case returns
the per-frame listing the detail page shows.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.output_frame_repository import IOutputFrameRepository
from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository
from serverV2.rendering.render_group.application.ports.storage_gateway import IStorageGateway


class GetOutputs:
    def __init__(
        self,
        *,
        group_repo: IRenderGroupRepository,
        storage: IStorageGateway,
        output_frames: IOutputFrameRepository,
    ) -> None:
        self._group_repo = group_repo
        self._storage = storage
        self._output_frames = output_frames

    def execute(self, group_id: str):
        raise NotImplementedError
