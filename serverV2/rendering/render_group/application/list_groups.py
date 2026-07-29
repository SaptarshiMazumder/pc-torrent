"""Retrieval: a user's render groups, paginated, optionally filtered.

Terminal groups are served straight off their frozen snapshot (written at
reconcile time) so the list never re-fetches children -- the repository
returns fully-formed ``RenderGroup`` entities and this use-case just applies
paging/filtering intent.
"""

from __future__ import annotations

from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository


class ListGroups:
    def __init__(self, *, group_repo: IRenderGroupRepository) -> None:
        self._group_repo = group_repo

    def execute(
        self, *, user_id: str, limit: int, offset: int, status_group: str | None
    ):
        raise NotImplementedError
