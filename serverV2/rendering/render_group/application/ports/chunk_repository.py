"""Port: read-only view of a group's chunks (jobs).

The rendering context does NOT own the chunk lifecycle -- the chunk concept is a peer
concern with its own writers (workers, allocation, the monitor).  Rendering
only READS chunk state, to roll the group's status up from it.  A repository
that only exposes reads keeps that ownership boundary explicit.
"""

from __future__ import annotations

from typing import Protocol

from serverV2.core.models import RenderJob


class IChunkRepository(Protocol):
    def get_by_group(self, group_id: str) -> list[RenderJob]: ...

    def group_id_for_job(self, job_id: str) -> str | None: ...
