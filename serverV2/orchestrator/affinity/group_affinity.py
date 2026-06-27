"""GroupAffinity — value object carrying a group's preferred placements.

The mirror of ``AntiAffinityExclusions``:

* ``preferred_serverless_capabilities`` — pairs of ``(fleet, gpu_type)`` that
  have STARTED or RENDERED at least one chunk of the group.  The planner
  prefers these on the group's retries (they demonstrably render this scene).
* ``preferred_machine_ids`` — community machine IDs that have started/rendered
  a chunk of the group.

Group-wide and growing: any chunk a combo rendered makes that combo affine
for every *other* chunk's retries.  Empty when nothing has started yet.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroupAffinity:
    preferred_serverless_capabilities: tuple[tuple[str, str], ...]
    preferred_machine_ids: tuple[str, ...]
