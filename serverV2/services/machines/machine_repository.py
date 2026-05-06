"""MachineRepository — all SQL for the ``machines`` table.

After Phase 1 of the allocator redesign, this repository is community-only.
Modal and Vast capabilities live in config.json and are never persisted as
machine rows.  Any leftover serverless rows from before the migration are
filtered out by ``machine_type = 'windows'`` on every query.

Composes ``MachineRedisMirror`` so every status mutation writes PG (sync)
and the Redis status hash (async via the mirror's executor) in lockstep.
PG remains the source of truth on disk; Redis is the fast read path used
by the allocator and the heartbeat self-heal in
``JobService.next_for_machine``.

Liveness lives entirely in Redis (``MachineRedisMirror.record/alive_ids``)
-- this layer handles only persistent state (status, GPU specs,
registration time).  ``last_seen_at`` column is kept in the schema for
historical / informational reads but no longer used as the live signal.
"""

from __future__ import annotations

from datetime import datetime, timezone

from serverV2.core.models import CommunityMachine
from serverV2.infrastructure.db import execute, query_all, query_one
from serverV2.services.machines.machine_redis_mirror import MachineRedisMirror


class MachineRepository:

    def __init__(
        self,
        mirror: MachineRedisMirror,
        stale_seconds: int = 15,
        community_price_per_hour: float = 1.0,
    ) -> None:
        self._mirror = mirror
        self._stale_seconds = stale_seconds
        # Injected once at startup from config.json's ``community.price_per_hour``.
        # Stamped onto every CommunityMachine this repo returns so cost-aware
        # allocators have a price to read without re-querying config.
        self._community_price = community_price_per_hour

    # ------------------------------------------------------------------
    # reads -- PG only (callers wanting the cached path go through the
    # mirror directly)
    # ------------------------------------------------------------------

    def get_available_community(self) -> list[CommunityMachine]:
        """All community machines whose persistent status is ``available``.
        PG-only fallback path -- used when the Redis status cache is
        unavailable.  Allocator's hot path goes through
        ``MachineRedisMirror.available_ids()`` paired with
        ``get_community_by_ids`` to avoid this WHERE-status scan.
        """
        rows = query_all(
            """
            SELECT * FROM machines
            WHERE status = 'available'
              AND machine_type = 'windows'
            ORDER BY gpu_vram_gb DESC
            """,
        )
        return [CommunityMachine.from_row(r, price_per_hour=self._community_price) for r in rows]

    def get_community_by_ids(self, machine_ids: set[str] | list[str]) -> list[CommunityMachine]:
        """Fetch full community-machine rows for the given ids.  Used by
        the resource picker after it gets the available-ids set from
        the Redis status cache.  Filters to machine_type='windows' so
        a stale Redis entry pointing at a non-community row never
        slips through."""
        ids_list = list(machine_ids)
        if not ids_list:
            return []
        rows = query_all(
            """
            SELECT * FROM machines
            WHERE id = ANY(%s)
              AND machine_type = 'windows'
            ORDER BY gpu_vram_gb DESC
            """,
            (ids_list,),
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

    def get_status(self, machine_id: str) -> str | None:
        """PG-side status read.  Allocator/self-heal hot paths use
        ``MachineRedisMirror.get_status`` first; this is the fallback."""
        row = query_one(
            "SELECT status FROM machines WHERE id = %s",
            (machine_id,),
        )
        return row["status"] if row else None

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

    # ------------------------------------------------------------------
    # writes -- PG sync, Redis mirror async
    # ------------------------------------------------------------------

    def update_status(self, machine_id: str, status: str) -> None:
        """Status mutation.  Single entry point for PG status writes;
        mirrors the value into Redis on a background executor so the
        caller's latency is unaffected by Redis I/O.  Stamps
        ``last_seen_at`` on flips to 'available' so the legacy column
        stays informative."""
        if status == "available":
            now = datetime.now(timezone.utc).isoformat()
            execute(
                "UPDATE machines SET status = %s, last_seen_at = %s WHERE id = %s",
                (status, now, machine_id),
            )
        else:
            execute(
                "UPDATE machines SET status = %s WHERE id = %s",
                (status, machine_id),
            )
        self._mirror.set_status(machine_id, status)

    def demote_ghosts(self, alive_ids: set[str]) -> int:
        """Demote 'available' community machines whose ids are NOT in
        ``alive_ids`` (Redis liveness set) back to 'idle'.  Catches
        agents that crashed without calling set_idle, so the status
        column doesn't permanently lie about reality.

        Only ``status='available'`` machines are touched.  ``processing``
        machines are exempt because their liveness during render flows
        through the active job's heartbeat (Redis ``job:{id}:hb``), not
        through ``machines:alive``.  Demoting them here would clobber a
        currently-rendering machine's status mid-job.

        Mirrors each demotion to Redis via the mirror's async path so
        the allocator's status cache stays consistent.

        Called periodically by CommunityMonitor.  Returns count demoted.
        """
        # Postgres handles empty ANY() arrays poorly; pad with a sentinel
        # that won't match any real machine_id.  '__none__' is a UUID-shaped
        # value that no real id will ever take.
        ids_param = list(alive_ids) if alive_ids else ["__none__"]
        result = query_all(
            """
            UPDATE machines SET status = 'idle'
            WHERE status = 'available'
              AND machine_type = 'windows'
              AND id != ALL(%s)
            RETURNING id
            """,
            (ids_param,),
        )
        if not result:
            return 0
        for row in result:
            self._mirror.set_status(row["id"], "idle")
        return len(result)
