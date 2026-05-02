"""MonitorLockCommunitySweepStrategy — orphan reclaim for the community singleton.

Structurally different from Vast/Modal: there's exactly one community
monitor across the whole fleet (gated by ``monitor:community``), not
one per job.  Per tick we just call ``try_start`` -- the lock acquire
inside that method decides whether THIS instance now runs the scan
thread.  No DB query needed because the community monitor itself
enumerates active community jobs every tick once it's running.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serverV2.fleets.community.community_monitor import CommunityMonitor


class MonitorLockCommunitySweepStrategy:

    def __init__(self, *, community_monitor: "CommunityMonitor") -> None:
        self._community_monitor = community_monitor

    def sweep(self) -> None:
        self._community_monitor.try_start()
