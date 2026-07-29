"""Adapter: ``IRenderGroupRepository`` backed by Postgres.

Implements the rendering port using the shared DB driver and maps rows to
``RenderGroup`` domain entities (``RenderGroup.from_row``).  This is where
column names are allowed to exist -- and nowhere inward of here.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import RenderGroup
from serverV2.rendering.render_group.application.ports.render_group_repository import IRenderGroupRepository


class RenderGroupRepository(IRenderGroupRepository):
    def __init__(self, db: Any) -> None:
        self._db = db

    def get_by_id(self, group_id: str) -> RenderGroup | None:
        raise NotImplementedError

    def list_for_user(
        self, user_id: str, *, limit: int, offset: int, status_group: str | None
    ) -> list[RenderGroup]:
        raise NotImplementedError

    def create(self, group: RenderGroup) -> None:
        raise NotImplementedError

    def update_status(self, group_id: str, status: str) -> None:
        raise NotImplementedError

    def write_terminal_snapshot(
        self, group_id: str, *, status: str, total_rendered: int, total_cost_usd: float
    ) -> None:
        raise NotImplementedError

    def delete(self, group_id: str) -> None:
        raise NotImplementedError
