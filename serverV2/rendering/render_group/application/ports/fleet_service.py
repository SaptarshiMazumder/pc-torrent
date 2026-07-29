"""Port: what rendering needs from the FLEET concern.

Rendering never talks to Vast/Modal/community SDKs directly.  When a group
is cancelled it asks, through this port, for every in-flight chunk of the
group to be torn down at the fleet level.
"""

from __future__ import annotations

from typing import Protocol


class IFleetService(Protocol):
    def cancel_all_for_group(self, group_id: str) -> int: ...
