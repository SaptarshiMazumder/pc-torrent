"""AntiAffinityRepository — I/O for the anti-affinity module.

Owns one focused query: read every prior terminal sibling of a chunk so the
service layer can build the exclusion set.  We don't borrow
``JobRepository.get_raw_by_group`` because that returns whole rows for the
whole group (wrong shape, more data than needed); anti-affinity asks a
specific question and owns the SQL for it.

Terminal here means ``status IN ('failed', 'cancelled')``.  ``done`` rows
contribute nothing (they succeeded — no anti-affinity signal).  Active
siblings (``pending`` / ``running``) are also excluded; concurrent dispatch
protection is the in-progress ledger's job, not anti-affinity's.
"""

from __future__ import annotations

from typing import Any

from serverV2.infrastructure.db import query_all


class AntiAffinityRepository:

    def get_terminal_siblings(
        self, group_id: str, chunk_index: int,
    ) -> list[dict[str, Any]]:
        """Return ``(machine_type, gpu_type, machine_id)`` for every prior
        failed/cancelled attempt of this chunk.  Empty list if no history."""
        return query_all(
            """
            SELECT machine_type, gpu_type, machine_id
            FROM jobs
            WHERE group_id = %s
              AND chunk_index = %s
              AND status IN ('failed', 'cancelled')
            """,
            (group_id, chunk_index),
        )
