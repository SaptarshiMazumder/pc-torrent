"""Affinity module — derives a group's preferred placements from job history.

Sibling of ``orchestrator/anti_affinity``: where anti-affinity hard-excludes
combos that FAILED a chunk, affinity prefers combos that STARTED/RENDERED any
chunk of the group.  Resolved fresh at plan time by the daemon.
"""

from serverV2.orchestrator.affinity.affinity_facade import AffinityFacade
from serverV2.orchestrator.affinity.affinity_repository import AffinityRepository
from serverV2.orchestrator.affinity.affinity_service import AffinityService
from serverV2.orchestrator.affinity.group_affinity import GroupAffinity

__all__ = [
    "AffinityFacade",
    "AffinityRepository",
    "AffinityService",
    "GroupAffinity",
]
