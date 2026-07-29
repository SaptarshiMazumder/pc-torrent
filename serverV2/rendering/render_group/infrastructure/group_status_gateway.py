"""Adapter: ``IGroupStatusGateway`` backed by the live Redis mirror.

Refreshes the active-group mirror off the render path (fire-and-forget) so
the live UI reflects per-chunk changes.  Terminal groups are dropped from
the mirror.  The use-cases call ``group_changed`` and neither know nor care
that Redis is behind it.
"""

from __future__ import annotations

from typing import Any

from serverV2.rendering.render_group.application.ports.group_status_gateway import IGroupStatusGateway


class GroupStatusGateway(IGroupStatusGateway):
    def __init__(self, cache: Any) -> None:
        self._cache = cache

    def group_changed(self, group_id: str) -> None:
        raise NotImplementedError
