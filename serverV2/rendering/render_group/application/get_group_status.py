"""Retrieval: current status of one render group.

Loads the group plus its chunk states and rendered-frame count through
ports and returns a domain-level view.  Shaping into the wire DTO is the
presenter's job, not this use-case's -- retrieval returns data, not JSON.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.chunk_repository import IChunkRepository
from serverV2.rendering.render_group.application.ports.output_frame_repository import IOutputFrameRepository
from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository


class GetGroupStatus:
    def __init__(
        self,
        *,
        group_repo: IRenderGroupRepository,
        chunk_repo: IChunkRepository,
        output_frames: IOutputFrameRepository,
    ) -> None:
        self._group_repo = group_repo
        self._chunk_repo = chunk_repo
        self._output_frames = output_frames

    def execute(self, group_id: str):
        raise NotImplementedError
