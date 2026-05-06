"""Job termination context + dependency bundle.

Carries everything a termination pipeline's steps need to run -- the job
row, the per-flow error string, the lifecycle's repos and helpers, the
pre-resolved anti-affinity exclusions for any retry the pipeline might
trigger, and a small set of mutable flags the steps write to as they
progress (whether the atomic-CAS dedup was won, whether a retry got
dispatched).

Steps read and write the context.  The pipeline never executes anything
itself; that's ``JobTerminator``'s job.  See lifecycle_job_termination/
__init__.py for the rationale.

Anti-affinity is resolved by ``RenderLifecycle`` BEFORE invoking the
pipeline and threaded in via ``exclusions``.  The pipeline itself
never queries job history for exclusions -- the resolver lives outside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.anti_affinity import AntiAffinityExclusions
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository
from serverV2.services.machines.machine_repository import MachineRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.lifecycle_job_retry import RetryExecutor


@dataclass
class LifecycleDeps:
    """Bundle of repositories and helpers a termination pipeline's steps
    use.  Constructed once in bootstrap, threaded into ``JobTerminator``.
    Steps read fields off this bundle via ``ctx.deps.<name>``.
    """

    job_repo: JobRepository
    group_repo: RenderGroupRepository
    in_progress_repo: InProgressChunkRepository
    machine_repo: MachineRepository
    fleet_registry: FleetRegistry
    retry_executor: "RetryExecutor"
    # Callable into RenderLifecycle.reconcile_group_status -- kept as a
    # callback so the termination package doesn't import lifecycle.
    reconcile_group: Callable[[str], None]


def _empty_exclusions() -> AntiAffinityExclusions:
    return AntiAffinityExclusions(
        excluded_serverless_capabilities=(),
        excluded_machine_ids=(),
    )


@dataclass
class TerminationContext:
    """One context object per termination call.  Steps mutate the
    ``we_own_retry`` and ``retried`` flags as they run; subsequent
    steps and conditional logic read them.
    """

    job_id: str
    raw: dict[str, Any]
    error: str
    deps: LifecycleDeps

    # Pre-resolved anti-affinity exclusions for any retry this pipeline
    # might trigger.  Defaults to empty so call sites that don't trigger
    # retries (group-level cancel) don't have to construct one.
    exclusions: AntiAffinityExclusions = field(default_factory=_empty_exclusions)

    # Flags written by steps, read by later steps.
    we_own_retry: bool = False
    retried: bool = False
    result: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # row-derived shortcuts so steps don't repeat the .get() boilerplate
    # ------------------------------------------------------------------

    @property
    def group_id(self) -> str:
        return self.raw.get("group_id") or ""

    @property
    def chunk_index(self) -> int:
        return self.raw.get("chunk_index") or 0

    @property
    def fleet(self) -> str:
        return self.raw.get("machine_type") or ""

    @property
    def machine_id(self) -> str | None:
        return self.raw.get("machine_id")
