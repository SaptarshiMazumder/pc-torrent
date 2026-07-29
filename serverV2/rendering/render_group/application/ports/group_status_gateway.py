"""Port: fire-and-forget notification that a group changed.

Backed by the live Redis mirror off the render path.  Every reconcile
calls this so the live UI reflects per-chunk changes even when the rolled-up
group status did not move; terminal groups get dropped from the mirror.
The use-case does not care how -- it just announces "this group changed".
"""

from __future__ import annotations

from typing import Protocol


class IGroupStatusGateway(Protocol):
    def group_changed(self, group_id: str) -> None: ...
