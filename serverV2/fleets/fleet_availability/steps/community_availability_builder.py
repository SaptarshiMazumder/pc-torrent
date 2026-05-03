"""CommunityAvailabilityBuilder — community-fleet availability.

Returns the set of community machines that are actually available
right now: machines whose persistent status is ``available`` AND whose
agent is currently pinging (alive within ``stale_seconds``).

Mirrors the community-resolution logic in ``bootstrap._resource_picker``
exactly, so swapping callers from the picker to this builder produces
the same machine list.

Two Redis signals, both consulted:

  1. ``machines:status`` hash via ``available_ids()`` -- which agents
     have ``status='available'`` cached.
  2. ``machines:alive`` sorted set via ``alive_ids(window)`` -- which
     agents are pinging within the stale window.

Available community pool = (status=available) ∩ (alive within window).

Fail-soft on Redis: each signal independently falls back when the
corresponding Redis read returns ``None``.  If status-cache is down,
we go to Postgres for ``WHERE status='available'``.  If alive-set is
down, we skip the alive intersect for this one tick (broader list,
slightly stale, acceptable per the existing picker's docstring).
"""

from __future__ import annotations

from serverV2.core.models import CommunityMachine
from serverV2.services.machines.machine_heartbeat_repository import (
    MachineHeartbeatRepository,
)
from serverV2.services.machines.machine_repository import MachineRepository


class CommunityAvailabilityBuilder:

    def __init__(
        self,
        *,
        machine_repo: MachineRepository,
        machine_heartbeat_repo: MachineHeartbeatRepository,
        stale_seconds: int,
    ) -> None:
        self._machines = machine_repo
        self._heartbeat = machine_heartbeat_repo
        self._stale_seconds = stale_seconds

    def build(self) -> tuple[CommunityMachine, ...]:
        # Step 1 -- status=available cohort, Redis-first with PG fallback.
        available_ids = self._heartbeat.available_ids()
        if available_ids is not None:
            community = self._machines.get_community_by_ids(available_ids)
        else:
            community = self._machines.get_available_community()
        # Step 2 -- intersect with the alive set (Redis-only; if Redis
        # is down we keep the broader status=available list for this
        # tick rather than mass-failing every dispatch).
        alive_ids = self._heartbeat.alive_ids(self._stale_seconds)
        if alive_ids is not None:
            community = [m for m in community if m.id in alive_ids]
        return tuple(community)
