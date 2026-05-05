"""AntiAffinityService -- pure orchestration layer.

Takes terminal-sibling rows from the repository and produces the unioned
``AntiAffinityExclusions`` value object.  Reads only the row fields it
needs; no I/O, no DB.

Per-row classification:

  * a serverless row contributes ``(fleet, gpu_type)`` to capability
    exclusions
  * a row with ``machine_id`` contributes that machine_id to id
    exclusions
  * a row with neither contributes nothing

Infrastructure-failure filter
-----------------------------

Some failures happen BEFORE the worker actually executes any work --
the dispatch call itself errored, the peer's container failed to
launch, the peer disappeared.  Treating these as anti-affinity signal
is wrong: nothing was learned about whether THIS gpu_type can render
THIS scene, because the worker never got to try.  Such siblings get
silently skipped from the exclusion union; the chunk's next retry sees
the full pool again.

Patterns matched are anchored to the start of the error string and
written by the fleet strategies and monitors at write time.  Runtime
failures (heartbeat dead, machine went offline, render runtime errors)
are intentionally NOT in the list -- those still contribute exclusions
because they may legitimately implicate the GPU/peer.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from serverV2.orchestrator.anti_affinity.anti_affinity_exclusions import (
    AntiAffinityExclusions,
)

log = logging.getLogger(__name__)


_SERVERLESS_FLEETS = frozenset({"modal_serverless", "vast_serverless"})

# Errors anchored to the start of these patterns mean "the worker never
# executed" -- so they don't tell us anything about the GPU class and
# should not contribute to anti-affinity.  Add new patterns here when a
# new write-time error string is introduced upstream.
_INFRASTRUCTURE_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^Dispatch failed:"),
    re.compile(r"^Vast\.ai fatal startup error"),
    re.compile(r"^Vast\.ai instance disappeared"),
    re.compile(r"^Vast\.ai instance stuck in"),
)


def _is_infrastructure_failure(error: str | None) -> bool:
    if not isinstance(error, str) or not error:
        return False
    return any(p.match(error) for p in _INFRASTRUCTURE_FAILURE_PATTERNS)


class AntiAffinityService:

    def build_exclusions(
        self, rows: list[dict[str, Any]],
    ) -> AntiAffinityExclusions:
        caps: set[tuple[str, str]] = set()
        ids: set[str] = set()
        skipped = 0
        for row in rows:
            error = row.get("error")
            if _is_infrastructure_failure(error):
                skipped += 1
                continue
            fleet = (row.get("machine_type") or "").strip()
            gpu_type = (row.get("gpu_type") or "").strip()
            machine_id = (row.get("machine_id") or "").strip()
            if fleet in _SERVERLESS_FLEETS and gpu_type:
                caps.add((fleet, gpu_type))
            elif machine_id:
                ids.add(machine_id)
        if skipped:
            log.info(
                "AntiAffinity: ignored %d sibling(s) for exclusion build "
                "(infrastructure-only failures don't blame the GPU class)",
                skipped,
            )
        return AntiAffinityExclusions(
            excluded_serverless_capabilities=tuple(sorted(caps)),
            excluded_machine_ids=tuple(sorted(ids)),
        )
