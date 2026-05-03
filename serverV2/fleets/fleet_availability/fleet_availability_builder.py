"""FleetAvailabilityBuilder — per-request fluent composer.

Constructed by ``FleetAvailabilityBuilderFactory.new()`` for each
caller; never shared across requests, so flag state cannot leak
between callers.

Usage (in the frame allocator / retry path, once wired up):

    snapshot = (
        factory.new()
        .check_vast()
        .check_community()
        # skipping .check_modal() — Modal can't run EEVEE
        .build()
    )

The flag is the "actually go ask the provider" signal — only fleets
the caller flagged get queried.  Unflagged fleets land in the snapshot
with an empty tuple, not the fleet's configured set; that lets a
caller distinguish "didn't check" from "checked, nothing live."

The per-fleet builders this class composes are pre-constructed at
boot and shared across requests via the factory; only the per-request
flag state lives on instances of this class.
"""

from __future__ import annotations

from serverV2.fleets.fleet_availability.fleet_availability_snapshot import (
    FleetAvailabilitySnapshot,
)
from serverV2.fleets.fleet_availability.steps import (
    CommunityAvailabilityBuilder,
    InProgressServerlessFleetBuilder,
    ModalAvailabilityBuilder,
    VastAvailabilityBuilder,
)


class FleetAvailabilityBuilder:

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
        self._check_vast = False
        self._check_modal = False
        self._check_community = False
        self._check_in_progress_serverless_fleet = False

    def check_vast(self) -> "FleetAvailabilityBuilder":
        self._check_vast = True
        return self

    def check_modal(self) -> "FleetAvailabilityBuilder":
        self._check_modal = True
        return self

    def check_community(self) -> "FleetAvailabilityBuilder":
        self._check_community = True
        return self

    def check_in_progress_serverless_fleet(self) -> "FleetAvailabilityBuilder":
        self._check_in_progress_serverless_fleet = True
        return self

    def build(self) -> FleetAvailabilitySnapshot:
        return FleetAvailabilitySnapshot(
            vast_available=self._vast.build() if self._check_vast else (),
            modal_available=self._modal.build() if self._check_modal else (),
            community_available=(
                self._community.build() if self._check_community else ()
            ),
            serverless_in_flight=(
                self._in_progress_serverless.build()
                if self._check_in_progress_serverless_fleet
                else {}
            ),
        )
