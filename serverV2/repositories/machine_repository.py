"""MachineRepository — all SQL for the ``machines`` table.

After Phase 1 of the allocator redesign, this repository is community-only.
Modal and Vast capabilities live in config.json and are never persisted as
machine rows.  Any leftover serverless rows from before the migration are
filtered out by ``machine_type = 'windows'`` on every query.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from serverV2.infrastructure.db import execute, query_all, query_one
from serverV2.core.models import CommunityMachine


class MachineRepository:

    def __init__(self, stale_seconds: int = 15, community_price_per_hour: float = 1.0) -> None:
        self._stale_seconds = stale_seconds
        # Injected once at startup from config.json's ``community.price_per_hour``.
        # Stamped onto every CommunityMachine this repo returns so cost-aware
        # allocators have a price to read without re-querying config.
        self._community_price = community_price_per_hour

    def get_available_community(self) -> list[CommunityMachine]:
        cutoff = self._cutoff()
        execute(
            """
            UPDATE machines SET status = 'idle'
            WHERE status IN ('available', 'processing')
              AND machine_type = 'windows'
              AND (last_seen_at IS NULL OR last_seen_at < %s)
            """,
            (cutoff,),
        )
        rows = query_all(
            """
            SELECT * FROM machines
            WHERE status = 'available'
              AND machine_type = 'windows'
              AND last_seen_at >= %s
            ORDER BY gpu_vram_gb DESC
            """,
            (cutoff,),
        )
        return [CommunityMachine.from_row(r, price_per_hour=self._community_price) for r in rows]

    def get_by_id(self, machine_id: str) -> CommunityMachine | None:
        row = query_one(
            "SELECT * FROM machines WHERE id = %s AND machine_type = 'windows'",
            (machine_id,),
        )
        return CommunityMachine.from_row(row, price_per_hour=self._community_price) if row else None

    def get_raw_by_ids(self, machine_ids: list[str]) -> dict[str, dict]:
        """Return raw rows keyed by machine id.  Missing ids are omitted.
        Used by serializers that resolve ``jobs.machine_id`` → display label.
        """
        if not machine_ids:
            return {}
        rows = query_all(
            "SELECT * FROM machines WHERE id = ANY(%s)", (list(machine_ids),),
        )
        return {r["id"]: r for r in rows}

    def set_processing(self, machine_id: str) -> None:
        execute("UPDATE machines SET status = 'processing' WHERE id = %s", (machine_id,))

    def set_available(self, machine_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        execute(
            "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
            (now, machine_id),
        )

    def mark_stale(self) -> None:
        execute(
            """
            UPDATE machines SET status = 'idle'
            WHERE status IN ('available', 'processing')
              AND machine_type = 'windows'
              AND (last_seen_at IS NULL OR last_seen_at < %s)
            """,
            (self._cutoff(),),
        )

    def get_failover_candidates(self, exclude_machine_id: str) -> list[CommunityMachine]:
        rows = query_all(
            """
            SELECT * FROM machines
            WHERE status = 'available' AND machine_type = 'windows' AND id != %s
            ORDER BY gpu_vram_gb DESC
            """,
            (exclude_machine_id,),
        )
        return [CommunityMachine.from_row(r, price_per_hour=self._community_price) for r in rows]

    def _cutoff(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=self._stale_seconds)).isoformat()
