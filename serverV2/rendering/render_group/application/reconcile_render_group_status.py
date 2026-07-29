"""Use-case: recompute a group's status from its chunks and persist it.

THE exemplar use-case -- the single chokepoint through which every source
(user command, worker callback, monitor tick) funnels group-status change.
Nothing else mutates ``render_groups.status`` outside the cancel path.

Shape (this is the template every use-case in the context follows):

    LOAD     group + chunk states + rendered count + in-flight allocation
             ... all through PORTS, no SQL here
    DECIDE   hand them to the domain rule ``compute_group_status``
             ... the use-case makes NO status decision itself
    PERSIST  if the rule says persist, write the status; if terminal, also
             roll up the cost (domain) and write the terminal snapshot
             ... through PORTS
    NOTIFY   announce the group changed so the live mirror refreshes
             ... through a PORT

Note the constructor: the use-case depends ONLY on abstractions.  Swap the
adapters for fakes and this whole flow runs with no DB, no fleet, no HTTP.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.allocation_service import IAllocationService
from serverV2.rendering.render_group.application.ports.group_status_gateway import IGroupStatusGateway
from serverV2.rendering.render_group.application.ports.chunk_repository import IChunkRepository
from serverV2.rendering.render_group.application.ports.output_frame_repository import IOutputFrameRepository
from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository


class ReconcileRenderGroupStatus:
    def __init__(
        self,
        *,
        group_repo: IRenderGroupRepository,
        chunk_repo: IChunkRepository,
        output_frames: IOutputFrameRepository,
        allocation: IAllocationService,
        notifier: IGroupStatusGateway,
    ) -> None:
        self._group_repo = group_repo
        self._chunk_repo = chunk_repo
        self._output_frames = output_frames
        self._allocation = allocation
        self._notifier = notifier

    def execute(self, group_id: str) -> None:
        raise NotImplementedError
