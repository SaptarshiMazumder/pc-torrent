"""Port: count of rendered output frames for a group.

Used by the status rollup (rendered vs total) and by the terminal
snapshot.  Reads only -- the outputs themselves are written elsewhere
when workers register their results.
"""

from __future__ import annotations

from typing import Protocol


class IOutputFrameRepository(Protocol):
    def count_for_group(self, group_id: str) -> int: ...
