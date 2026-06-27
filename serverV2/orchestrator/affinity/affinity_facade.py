"""AffinityFacade — public entry point for the affinity module.

The mirror of ``AntiAffinityFacade``: composes ``AffinityRepository`` +
``AffinityService`` behind one method.  Callers depend on this class only
(never the repo or service directly) so the module is free to evolve.

Unlike anti-affinity, which is resolved at FAILURE time (it needs the
in-flight failing row), affinity is a clean group query with no failure-time
dependency -- so it is resolved fresh by the daemon at plan time (the
``AllocationPendingTickProcessor`` calls ``affinity_for_group`` as it builds
each retry chunk request).
"""

from __future__ import annotations

from serverV2.orchestrator.affinity.affinity_repository import AffinityRepository
from serverV2.orchestrator.affinity.affinity_service import AffinityService
from serverV2.orchestrator.affinity.group_affinity import GroupAffinity


class AffinityFacade:

    def __init__(
        self,
        *,
        repository: AffinityRepository,
        service: AffinityService,
    ) -> None:
        self._repository = repository
        self._service = service

    def affinity_for_group(self, group_id: str) -> GroupAffinity:
        """Preferred placements for a group: every ``(fleet, gpu_type)`` /
        community machine that has started or rendered any chunk of the
        group.  Returns an empty ``GroupAffinity`` when nothing has started
        yet."""
        rows = self._repository.get_started_siblings(group_id)
        return self._service.build_affinity(rows)
