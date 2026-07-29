"""Adapter: ``IChunkRepository`` (read side) backed by Postgres.

Rendering reads chunk state to roll up the group; the writes to the jobs
table belong to the jobs concern, not here.  Maps rows to ``RenderJob``.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import RenderJob
from serverV2.rendering.render_group.application.ports.chunk_repository import IChunkRepository


class ChunkRepository(IChunkRepository):
    def __init__(self, db: Any) -> None:
        self._db = db

    def get_by_group(self, group_id: str) -> list[RenderJob]:
        raise NotImplementedError

    def group_id_for_job(self, job_id: str) -> str | None:
        raise NotImplementedError
