"""TerminalGroupResourceReleaser -- release downstream resources held
by a render group once the group reaches a terminal state.

A terminal group must not have queued work for it.  The dispatch and
pending allocation queues are the queues; the canonical release call
is ``AllocationClient.drain_for_group(group_id)``.

This class is the orchestrator's single home for that responsibility.
Today it just drains the two allocation queues; future cleanup that
should fire at the same trigger (monitor stops, ledger releases, etc.)
will land here too.

Self-gating: ``release(group_id)`` reads the current group status and
returns 0 when the group isn't terminal, so callers can invoke it
unconditionally after every ``reconcile_group_status`` call without
needing to branch on the new status themselves.

Layer rule: callers inside ``orchestrator/`` use this class directly.
Callers outside ``orchestrator/`` must go through
``RenderLifecycle.release_terminal_group_resources`` (the facade).
"""

from __future__ import annotations

import logging

from serverV2.clients.allocation_client import AllocationClient
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


_TERMINAL_GROUP_STATUSES = frozenset({"done", "failed", "cancelled"})


class TerminalGroupResourceReleaser:

    def __init__(
        self,
        *,
        allocation_client: AllocationClient,
        group_repo: RenderGroupRepository,
    ) -> None:
        self._allocation_client = allocation_client
        self._group_repo = group_repo

    def release(self, group_id: str) -> int:
        """Drain queued work for ``group_id`` iff the group is terminal.

        Returns the count of allocation rows drained (0 if the group
        isn't terminal, or if no rows were queued).
        """
        if not group_id:
            return 0
        group = self._group_repo.get_by_id(group_id)
        if not group:
            return 0
        status = str(group.get("status") or "")
        if status not in _TERMINAL_GROUP_STATUSES:
            return 0
        drained = self._allocation_client.drain_for_group(group_id)
        if drained:
            log.info(
                "Group %s (%s): drained %d allocation row(s) (dispatch + pending)",
                group_id, status, drained,
            )
        return drained
