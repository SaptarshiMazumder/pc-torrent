"""Port: persistence contract for the RenderGroup aggregate.

Owned by the rendering application layer; implemented by an adapter in
``rendering/infrastructure/db``.  Returns domain entities, never raw rows --
row-to-entity mapping is the adapter's job.

Cleaner than the legacy ``core.interfaces.IRenderGroupRepository`` (which
hands back ``dict[str, Any]``): this port speaks the ``RenderGroup``
domain entity, so use-cases never touch column names.
"""

from __future__ import annotations

from typing import Protocol

from serverV2.core.models import RenderGroup


class IRenderGroupRepository(Protocol):
    def get_by_id(self, group_id: str) -> RenderGroup | None: ...

    def list_for_user(
        self, user_id: str, *, limit: int, offset: int, status_group: str | None
    ) -> list[RenderGroup]: ...

    def create(self, group: RenderGroup) -> None: ...

    def update_status(self, group_id: str, status: str) -> None: ...

    def write_terminal_snapshot(
        self, group_id: str, *, status: str, total_rendered: int, total_cost_usd: float
    ) -> None: ...

    def delete(self, group_id: str) -> None: ...
