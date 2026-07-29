"""Adapter: ``IOutputFrameRepository`` backed by Postgres.

Counts registered output frames for a group.  Read-only from rendering's
side -- the writes happen when workers register their results in the jobs
concern.
"""

from __future__ import annotations

from typing import Any

from serverV2.rendering.render_group.application.ports.output_frame_repository import IOutputFrameRepository


class OutputFrameRepository(IOutputFrameRepository):
    def __init__(self, db: Any) -> None:
        self._db = db

    def count_for_group(self, group_id: str) -> int:
        raise NotImplementedError
