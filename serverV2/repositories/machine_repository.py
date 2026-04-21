"""MachineRepository — all SQL for the ``machines`` table."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from serverV2.infrastructure.db import execute, query_all, query_one
from serverV2.core.models import Machine


class MachineRepository:

    def __init__(self, stale_seconds: int = 15) -> None:
        self._stale_seconds = stale_seconds

    def get_available(self) -> list[Machine]:
        cutoff = self._cutoff()
        execute(
            """
            UPDATE machines SET status = 'idle'
            WHERE status = 'available'
              AND machine_type NOT IN ('modal_serverless', 'vast_serverless')
              AND (last_seen_at IS NULL OR last_seen_at < %s)
            """,
            (cutoff,),
        )
        rows = query_all(
            """
            SELECT * FROM machines
            WHERE status = 'available'
              AND (machine_type IN ('modal_serverless', 'vast_serverless')
                   OR last_seen_at >= %s)
            ORDER BY gpu_vram_gb DESC
            """,
            (cutoff,),
        )
        return [Machine.from_row(r) for r in rows]

    def get_by_id(self, machine_id: str) -> Machine | None:
        row = query_one("SELECT * FROM machines WHERE id = %s", (machine_id,))
        return Machine.from_row(row) if row else None

    def get_raw_by_ids(self, machine_ids: list[str]) -> dict[str, dict]:
        """Return raw rows keyed by machine id.  Missing ids are omitted."""
        if not machine_ids:
            return {}
        rows = query_all(
            "SELECT * FROM machines WHERE id = ANY(%s)", (list(machine_ids),),
        )
        return {r["id"]: r for r in rows}

    def get_type(self, machine_id: str) -> str:
        row = query_one("SELECT machine_type FROM machines WHERE id = %s", (machine_id,))
        return row["machine_type"] if row else "windows"

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
            WHERE status = 'available'
              AND machine_type NOT IN ('modal_serverless', 'vast_serverless')
              AND (last_seen_at IS NULL OR last_seen_at < %s)
            """,
            (self._cutoff(),),
        )

    def get_failover_candidates(self, exclude_machine_id: str) -> list[Machine]:
        rows = query_all(
            "SELECT * FROM machines WHERE status = 'available' AND id != %s ORDER BY gpu_vram_gb DESC",
            (exclude_machine_id,),
        )
        return [Machine.from_row(r) for r in rows]

    def _cutoff(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=self._stale_seconds)).isoformat()
