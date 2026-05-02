"""AntiAffinityService — pure orchestration layer.

Takes terminal-sibling rows from the repository and produces the unioned
``AntiAffinityExclusions`` value object.  No I/O, no DB, no logging — the
entire surface is one pure function over rows.

The per-row classification mirrors the previous single-row
``RetryDispatcher.exclusions_for`` semantics exactly: a serverless row
contributes a ``(fleet, gpu_type)`` capability exclusion; a row with a
``machine_id`` contributes a community machine-id exclusion; rows with
neither contribute nothing.  The only difference from the legacy code is
that this method runs across N rows and unions the result instead of
operating on a single row.
"""

from __future__ import annotations

from typing import Any

from serverV2.orchestrator.anti_affinity.anti_affinity_exclusions import (
    AntiAffinityExclusions,
)


_SERVERLESS_FLEETS = frozenset({"modal_serverless", "vast_serverless"})


class AntiAffinityService:

    def build_exclusions(
        self, rows: list[dict[str, Any]],
    ) -> AntiAffinityExclusions:
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
        return AntiAffinityExclusions(
            excluded_serverless_capabilities=tuple(sorted(caps)),
            excluded_machine_ids=tuple(sorted(ids)),
        )
