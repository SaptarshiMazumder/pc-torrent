"""AffinityService -- pure orchestration layer.

The mirror of ``AntiAffinityService``: takes started-sibling rows from the
repository and produces a ``GroupAffinity`` value object.  Reads only the
row fields it needs; no I/O, no DB.

Per-row classification (same surface as anti-affinity):

  * a serverless row contributes ``(fleet, gpu_type)`` to preferred caps
  * a row with ``machine_id`` contributes that machine_id to preferred ids
  * a row with neither contributes nothing

There is deliberately NO infrastructure-failure filter (anti-affinity's
carve-out): every row here already STARTED rendering, so it is by definition
a positive signal -- there is nothing to filter out.
"""

from __future__ import annotations

from typing import Any

from serverV2.orchestrator.affinity.group_affinity import GroupAffinity

_SERVERLESS_FLEETS = frozenset({"modal_serverless", "vast_serverless"})


class AffinityService:

    def build_affinity(self, rows: list[dict[str, Any]]) -> GroupAffinity:
        caps: set[tuple[str, str]] = set()
        ids: set[str] = set()
        for row in rows:
            fleet = (row.get("machine_type") or "").strip()
            gpu_type = (row.get("gpu_type") or "").strip()
            machine_id = (row.get("machine_id") or "").strip()
            if fleet in _SERVERLESS_FLEETS and gpu_type:
                caps.add((fleet, gpu_type))
            elif machine_id:
                ids.add(machine_id)
        return GroupAffinity(
            preferred_serverless_capabilities=tuple(sorted(caps)),
            preferred_machine_ids=tuple(sorted(ids)),
        )
