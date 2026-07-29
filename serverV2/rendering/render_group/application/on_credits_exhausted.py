"""Event handler: react to billing's ``CreditsExhausted`` signal.

The REVERSE cross-context edge from the bounded-contexts UML.  Billing does
NOT call rendering -- it publishes ``CreditsExhausted`` on the shared bus,
and rendering subscribes here.  The handler finds the user's still-active
groups and cancels them through rendering's OWN ``CancelRenderGroup`` use-case.

This is what keeps the dependency graph acyclic: forward influence is a port
call (rendering -> billing), reverse influence is an event (billing -> bus
-> rendering).  Neither context imports the other.

The event payload itself is billing's; rendering receives only the fields it
needs (``user_id``) -- decoded by the presentation/bus adapter, not here.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.cancel_render_group import CancelRenderGroup
from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository


class OnCreditsExhausted:
    def __init__(
        self,
        *,
        group_repo: IRenderGroupRepository,
        cancel_group: CancelRenderGroup,
    ) -> None:
        self._group_repo = group_repo
        self._cancel_group = cancel_group

    def handle(self, *, user_id: str) -> None:
        raise NotImplementedError
