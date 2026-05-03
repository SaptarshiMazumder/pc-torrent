"""Per-step builders composed by ``FleetAvailabilityBuilder``.

One file per step class.  Each builder owns one focused question:

* ``VastAvailabilityBuilder``           -- which Vast gpu_types have
                                            live offers right now
* ``ModalAvailabilityBuilder``          -- which Modal endpoints have
                                            cap headroom (per-gpu and
                                            fleet-wide)
* ``CommunityAvailabilityBuilder``      -- which community machines are
                                            currently available + alive
* ``InProgressServerlessFleetBuilder``  -- ``{fleet -> in_flight_count}``
                                            for allocator headroom math

Steps are stateless given their constructor deps; the master builder
fluent-flags which ones to actually invoke per request, then assembles
their results into a ``FleetAvailabilitySnapshot``.
"""

from serverV2.fleets.fleet_availability.steps.community_availability_builder import (
    CommunityAvailabilityBuilder,
)
from serverV2.fleets.fleet_availability.steps.in_progress_serverless_fleet_builder import (
    InProgressServerlessFleetBuilder,
)
from serverV2.fleets.fleet_availability.steps.modal_availability_builder import (
    ModalAvailabilityBuilder,
)
from serverV2.fleets.fleet_availability.steps.vast_availability_builder import (
    VastAvailabilityBuilder,
)

__all__ = [
    "CommunityAvailabilityBuilder",
    "InProgressServerlessFleetBuilder",
    "ModalAvailabilityBuilder",
    "VastAvailabilityBuilder",
]
