"""Fleet availability module — pre-flight check for "which fleets can
I actually dispatch to right now?"

Public surface:

* ``FleetAvailabilityBuilderFactory`` — constructed once at bootstrap
  with the three per-fleet builders.  Hands out fresh request-scoped
  builders via ``.new()``.
* ``FleetAvailabilityBuilder`` — request-scoped fluent composer.
  ``check_vast / check_modal / check_community`` flag fleets, then
  ``build()`` queries only the flagged ones and returns a snapshot.
* ``FleetAvailabilitySnapshot`` — value object returned by ``build()``.

The three per-fleet builders are also exported so bootstrap can
construct them with the appropriate deps:

* ``VastAvailabilityBuilder``      — HTTP probe of Vast ``/bundles/``
* ``ModalAvailabilityBuilder``     — DB-only ``count_active_by_fleet``
                                     vs ``max_parallel``
* ``CommunityAvailabilityBuilder`` — DB-only
                                     ``MachineRepository.get_available_community``

Today every per-fleet ``build()`` raises ``NotImplementedError``.
The shape is locked; each fleet's real query is filled in
incrementally without changing the bootstrap wiring or the caller
contract.
"""

from serverV2.fleets.fleet_availability.fleet_availability_builder import (
    FleetAvailabilityBuilder,
)
from serverV2.fleets.fleet_availability.fleet_availability_builder_factory import (
    FleetAvailabilityBuilderFactory,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot import (
    FleetAvailabilitySnapshot,
)
from serverV2.fleets.fleet_availability.steps import (
    CommunityAvailabilityBuilder,
    InProgressServerlessFleetBuilder,
    ModalAvailabilityBuilder,
    VastAvailabilityBuilder,
)

__all__ = [
    "CommunityAvailabilityBuilder",
    "FleetAvailabilityBuilder",
    "FleetAvailabilityBuilderFactory",
    "FleetAvailabilitySnapshot",
    "InProgressServerlessFleetBuilder",
    "ModalAvailabilityBuilder",
    "VastAvailabilityBuilder",
]
