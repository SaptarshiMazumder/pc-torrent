"""FleetAvailabilitySnapshot — value object capturing what's available
right now across all fleets the caller asked about.

Returned by ``FleetAvailabilityBuilder.build()``.  Populated only for
fleets / signals the caller flagged via ``check_vast`` /
``check_modal`` / ``check_community`` /
``check_in_progress_serverless_fleet``.  Unflagged signals get an
empty tuple (or empty dict) — caller distinguishes "didn't check"
from "checked, nothing live" by tracking which signals it requested.

Reuses ``FleetCapability`` and ``CommunityMachine`` from ``core.models``
so downstream consumers (frame allocator, retry pipeline) can adapt
the snapshot to ``AvailableResources`` directly without a shape
translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from serverV2.core.models import CommunityMachine, FleetCapability


@dataclass(frozen=True)
class FleetAvailabilitySnapshot:
    vast_available: tuple[FleetCapability, ...]
    modal_available: tuple[FleetCapability, ...]
    community_available: tuple[CommunityMachine, ...]
    serverless_in_flight: dict[str, int] = field(default_factory=dict)

    def to_mutable(self):
        # Local import — MutableFleetAvailabilitySnapshot imports this
        # module, so a top-level import here would be circular.
        from serverV2.fleets.fleet_availability.mutable_fleet_availability_snapshot import (
            MutableFleetAvailabilitySnapshot,
        )
        return MutableFleetAvailabilitySnapshot(
            vast_available=self.vast_available,
            modal_available=self.modal_available,
            community_available=self.community_available,
            serverless_in_flight=self.serverless_in_flight,
        )
