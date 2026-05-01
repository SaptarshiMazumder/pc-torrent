"""Job termination context + dependency bundle.

Carries everything a termination pipeline's steps need to run -- the job
row, the per-flow error string, the lifecycle's repos and helpers, and a
small set of mutable flags the steps write to as they progress (e.g.
whether the atomic-CAS dedup was won, whether a retry got dispatched).

Steps read and write the context.  The pipeline never executes anything
itself; that's ``JobTerminator``'s job.  See lifecycle_job_termination/
__init__.py for the rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

from serverV2.fleets.registry import FleetRegistry
from serverV2.orchestrator.dispatch.coordinator import DispatchCoordinator
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository
from serverV2.services.machines.machine_state_writer import MachineStateWriter

if TYPE_CHECKING:
    from serverV2.orchestrator.lifecycle_job_termination.execution.retry_dispatcher import (
        RetryDispatcher,
    )


@dataclass
class LifecycleDeps:
    """Bundle of repositories and helpers a termination pipeline's steps
    use.  Constructed once in bootstrap, threaded into ``JobTerminator``.
    Steps read fields off this bundle via ``ctx.deps.<name>``.
    """

    job_repo: JobRepository
    group_repo: RenderGroupRepository
    in_progress_repo: InProgressChunkRepository
    state_writer: MachineStateWriter
    fleet_registry: FleetRegistry
    coordinator: DispatchCoordinator
    retry_dispatcher: "RetryDispatcher"
    # Callable into RenderLifecycle.reconcile_group_status -- kept as a
    # callback so the termination package doesn't import lifecycle.
    reconcile_group: Callable[[str], None]


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
