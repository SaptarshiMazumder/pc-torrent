"""Anti-affinity module — produces the exclusion set a retry dispatch must
honor so a chunk is never re-routed to a (fleet, gpu_type) or community
machine where any prior attempt of that chunk failed or was cancelled.

Public surface is ``AntiAffinityFacade``; consumers should depend only on
it.  ``AntiAffinityExclusions`` is the returned value object.
"""

from serverV2.orchestrator.anti_affinity.anti_affinity_exclusions import (
    AntiAffinityExclusions,
)
from serverV2.orchestrator.anti_affinity.anti_affinity_facade import (
    AntiAffinityFacade,
)
from serverV2.orchestrator.anti_affinity.anti_affinity_repository import (
    AntiAffinityRepository,
)
from serverV2.orchestrator.anti_affinity.anti_affinity_service import (
    AntiAffinityService,
)

__all__ = [
    "AntiAffinityExclusions",
    "AntiAffinityFacade",
    "AntiAffinityRepository",
    "AntiAffinityService",
]
