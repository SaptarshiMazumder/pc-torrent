"""AntiAffinityFacade — public entry point for the anti-affinity module.

Composes ``AntiAffinityRepository`` + ``AntiAffinityService`` and exposes a
single method consumers depend on.  Constructor injection so tests can
swap in fakes for either layer.

Callers should depend on this class only — never on the repository or
service directly — so the module stays free to evolve its internals.
"""

from __future__ import annotations

from typing import Any

from serverV2.orchestrator.anti_affinity.anti_affinity_exclusions import (
    AntiAffinityExclusions,
)
from serverV2.orchestrator.anti_affinity.anti_affinity_repository import (
    AntiAffinityRepository,
)
from serverV2.orchestrator.anti_affinity.anti_affinity_service import (
    AntiAffinityService,
)


class AntiAffinityFacade:

    def __init__(
        self,
        *,
        repository: AntiAffinityRepository,
        service: AntiAffinityService,
    ) -> None:
        self._repository = repository
        self._service = service

    def exclusions_for_chunk(
        self,
        group_id: str,
        chunk_index: int,
        *,
        including_row: dict[str, Any] | None = None,
    ) -> AntiAffinityExclusions:
        """Build the union of anti-affinity exclusions for a chunk from
        every prior failed/cancelled sibling attempt.  Returns an empty
        ``AntiAffinityExclusions`` when the chunk has no failure history.

        ``including_row`` is the failing/cancelling job's dict-shaped
        row.  Failure and cancel callers (``handle_chunk_failed`` and
        ``cancel_one_job``) pass it because the row is still
        ``'running'`` / ``'pending'`` at the moment exclusions are
        resolved -- the pipeline hasn't marked it terminal yet, so the
        DB query alone would miss it.  Manual retry doesn't pass it:
        the failed row is already ``'failed'`` by the time the user
        clicks Retry, so the query naturally includes it.
        """
        rows = self._repository.get_terminal_siblings(group_id, chunk_index)
        if including_row is not None:
            rows = rows + [including_row]
        return self._service.build_exclusions(rows)
