"""MachineStateWriter — single writer for the ``machines.status`` field.

Every Postgres status mutation goes through here.  The writer mirrors the
update into Redis (``machines:status`` hash) so the allocator's read path
and the heartbeat-driven self-heal in ``JobService.next_for_machine``
can answer "is this machine available?" with a single Redis HGET / HGETALL
instead of a round-trip to Postgres.

Postgres remains the source of truth on disk -- if Redis goes down or
returns a miss, callers fall back to the PG row via
``MachineRepository.get_status`` / ``get_available_community``.

Why a writer instead of two direct calls at every site:

* Centralises the invariant "PG status == Redis status".  No site can
  forget the Redis mirror, no site can write Redis without also writing
  PG, both writes happen in the same call.
* Keeps ``MachineRepository`` SQL-only and ``MachineHeartbeatRepository``
  Redis-only (single-store SRP per repo).
* Lets us add cross-cutting concerns later (status-change audit log,
  metrics, validation) without rewriting every call site.
"""

from __future__ import annotations

import logging

from serverV2.services.machines.machine_heartbeat_repository import (
    MachineHeartbeatRepository,
)
from serverV2.services.machines.machine_repository import MachineRepository

log = logging.getLogger(__name__)


class MachineStateWriter:

    def __init__(
        self,
        *,
        machine_repo: MachineRepository,
        machine_heartbeat_repo: MachineHeartbeatRepository,
    ) -> None:
        self._machine_repo = machine_repo
        self._machine_hb = machine_heartbeat_repo

    def set_status(self, machine_id: str, status: str) -> None:
        """Update PG and mirror to Redis.  Both writes are best-effort
        from the caller's perspective: the PG update raises if it fails
        (caller sees an exception), the Redis mirror is silent on
        failure (Postgres is still authoritative).
        """
        self._machine_repo.update_status(machine_id, status)
        self._machine_hb.set_status(machine_id, status)
