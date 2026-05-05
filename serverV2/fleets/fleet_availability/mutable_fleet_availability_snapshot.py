"""MutableFleetAvailabilitySnapshot — tick-scoped mutable view.

The frozen ``FleetAvailabilitySnapshot`` is the cached value object.
The daemon's tick reads it, calls ``.to_mutable()``, threads the
mutable copy through the dispatch loop (each successful dispatch
mutates it), and at the end of the tick writes ``.to_frozen()`` back
to the Redis cache (preserving TTL).

Mutations the dispatch service makes after a successful dispatch:
  * Vast      — drop the consumed GPU offer if exhausted; bump
                ``serverless_in_flight["vast_serverless"]``
  * Modal     — bump ``serverless_in_flight["modal_serverless"]``;
                drop modal endpoint if its per-GPU cap is now hit
  * Community — drop the dispatched machine from
                ``community_available``

These mutations live on this class so the dispatch service doesn't
poke into the snapshot's internals directly.
"""

from __future__ import annotations

from typing import Iterable

from serverV2.core.models import CommunityMachine, FleetCapability
from serverV2.fleets.fleet_availability.fleet_availability_snapshot import (
    FleetAvailabilitySnapshot,
)


class MutableFleetAvailabilitySnapshot:

    def __init__(
        self,
        *,
        vast_available: Iterable[FleetCapability],
        modal_available: Iterable[FleetCapability],
        community_available: Iterable[CommunityMachine],
        serverless_in_flight: dict[str, int],
    ) -> None:
        self.vast_available: list[FleetCapability] = list(vast_available)
        self.modal_available: list[FleetCapability] = list(modal_available)
        self.community_available: list[CommunityMachine] = list(community_available)
        self.serverless_in_flight: dict[str, int] = dict(serverless_in_flight)

    # ------------------------------------------------------------------
    # mutations called by AllocationPendingTickProcessor after enqueue
    # ------------------------------------------------------------------

    def mark_vast_dispatched(self, gpu_type: str | None) -> None:
        self.serverless_in_flight["vast_serverless"] = (
            self.serverless_in_flight.get("vast_serverless", 0) + 1
        )
        if gpu_type:
            self.vast_available = [
                c for c in self.vast_available if c.gpu_type != gpu_type
            ]

    def mark_modal_dispatched(self, gpu_type: str | None) -> None:
        self.serverless_in_flight["modal_serverless"] = (
            self.serverless_in_flight.get("modal_serverless", 0) + 1
        )
        if gpu_type:
            self.modal_available = [
                c for c in self.modal_available if c.gpu_type != gpu_type
            ]

    def mark_community_dispatched(self, machine_id: str | None) -> None:
        if not machine_id:
            return
        self.community_available = [
            m for m in self.community_available if m.id != machine_id
        ]

    # ------------------------------------------------------------------
    # conversion
    # ------------------------------------------------------------------

    def to_frozen(self) -> FleetAvailabilitySnapshot:
        return FleetAvailabilitySnapshot(
            vast_available=tuple(self.vast_available),
            modal_available=tuple(self.modal_available),
            community_available=tuple(self.community_available),
            serverless_in_flight=dict(self.serverless_in_flight),
        )
