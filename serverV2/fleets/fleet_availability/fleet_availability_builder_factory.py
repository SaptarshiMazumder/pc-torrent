"""FleetAvailabilityBuilderFactory — produces fresh per-request builders.

Constructed once at bootstrap with the three per-fleet builders.
Each call to ``.new()`` returns a fresh ``FleetAvailabilityBuilder``
whose flag state starts clean -- no leakage of "which fleets to
check" between requests.

The per-fleet builders are shared (same ``VastClient``, same
``JobRepository``, same ``MachineRepository`` across requests).  Only
the request-scoped flag state lives on the per-request builder.
"""

from __future__ import annotations

from serverV2.fleets.fleet_availability.fleet_availability_builder import (
    FleetAvailabilityBuilder,
)
from serverV2.fleets.fleet_availability.steps import (
    CommunityAvailabilityBuilder,
    InProgressServerlessFleetBuilder,
    ModalAvailabilityBuilder,
    VastAvailabilityBuilder,
)


class FleetAvailabilityBuilderFactory:

    def __init__(
        self,
        *,
        vast: VastAvailabilityBuilder,
        modal: ModalAvailabilityBuilder,
        community: CommunityAvailabilityBuilder,
        in_progress_serverless_fleet: InProgressServerlessFleetBuilder,
    ) -> None:
        self._vast = vast
        self._modal = modal
        self._community = community
        self._in_progress_serverless = in_progress_serverless_fleet

    def new(self) -> FleetAvailabilityBuilder:
        return FleetAvailabilityBuilder(
            vast=self._vast,
            modal=self._modal,
            community=self._community,
            in_progress_serverless_fleet=self._in_progress_serverless,
        )
