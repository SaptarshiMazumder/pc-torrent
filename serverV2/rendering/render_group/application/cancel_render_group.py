"""Use-case: cancel a render group.

The one status mutation OUTSIDE the reconcile chokepoint -- an explicit
user (or admin) command.  Guards with ``can_cancel``, tears down every
in-flight chunk at the fleet level, marks the group ``cancelled``, and
announces the change.  Any credits reserved but unspent are refunded.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.credits_service import ICreditsService
from serverV2.rendering.render_group.application.ports.fleet_service import IFleetService
from serverV2.rendering.render_group.application.ports.group_status_gateway import IGroupStatusGateway
from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository


class CancelRenderGroup:
    def __init__(
        self,
        *,
        group_repo: IRenderGroupRepository,
        fleet: IFleetService,
        credits: ICreditsService,
        notifier: IGroupStatusGateway,
    ) -> None:
        self._group_repo = group_repo
        self._fleet = fleet
        self._credits = credits
        self._notifier = notifier

    def execute(self, group_id: str) -> dict:
        raise NotImplementedError
