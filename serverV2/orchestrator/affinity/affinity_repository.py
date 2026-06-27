"""AffinityRepository — I/O for the affinity module.

The mirror of ``AntiAffinityRepository``, with two inversions:

* It asks a GROUP-wide question (not per-chunk): which combos have started
  rendering anywhere in the group.
* It reads SUCCESS-ish rows, not failures: ``started_at IS NOT NULL`` means
  the worker got past dispatch and pushed its first PROGRESS callback -- a
  strong "this gpu_type / machine can actually render this scene" signal.
  ``done`` rows naturally qualify too (they have a ``started_at``).

We don't borrow ``JobRepository.get_raw_by_group`` because that returns whole
rows for the whole group; affinity asks one focused question and owns its SQL.
"""

from __future__ import annotations

from typing import Any

from serverV2.infrastructure.db import query_all


class AffinityRepository:

    def get_started_siblings(self, group_id: str) -> list[dict[str, Any]]:
        """Return ``(machine_type, gpu_type, machine_id)`` for every job in
        the group that started rendering (``started_at IS NOT NULL``).  Empty
        list when nothing has started yet (e.g. the group's first attempt),
        in which case affinity is empty and the planner falls back to the
        normal fleet-share selection."""
        return query_all(
            """
            SELECT DISTINCT machine_type, gpu_type, machine_id
            FROM jobs
            WHERE group_id = %s
              AND started_at IS NOT NULL
            """,
            (group_id,),
        )
