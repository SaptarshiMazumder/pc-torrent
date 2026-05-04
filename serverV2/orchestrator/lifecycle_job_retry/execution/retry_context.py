"""RetryContext + RetryDeps — per-call state and the dependency bundle.

Mirrors the shape of ``TerminationContext``/``LifecycleDeps`` in the
sibling termination package.  The executor constructs one
``RetryContext`` per pipeline run; steps read and mutate fields on it
to communicate with each other.

Field discipline:

* ``raw``, ``error``, ``exclusions``, ``deps`` — set at construction by
  the executor; never mutated by steps.
* ``aborted`` — auto-retry steps set this to silently short-circuit
  downstream steps (matches today's ``return False`` shape).  Manual
  retry doesn't use this flag — its abort path is exception-based.
* All other fields — populated by individual steps as the pipeline
  progresses.  Each step's docstring documents what it reads and writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.core.models import (
    PlannedTask,
    RenderJob,
)
from serverV2.orchestrator.allocation_client import AllocationClient
from serverV2.orchestrator.anti_affinity import AntiAffinityExclusions
from serverV2.orchestrator.chunk_progress import ChunkProgressService
from serverV2.repositories.in_progress_chunk_repository import (
    InProgressChunkRepository,
)
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository


@dataclass
class RetryDeps:
    """Repositories and helpers a retry pipeline's steps need.  Constructed
    once at boot and threaded into ``RetryExecutor``.  Steps read fields
    via ``ctx.deps.<name>`` — never import their own repos.
    """

    job_repo: JobRepository
    group_repo: RenderGroupRepository
    chunk_progress: ChunkProgressService
    in_progress_repo: InProgressChunkRepository
    allocation_client: AllocationClient
    # Callable into RenderLifecycle.reconcile_group_status — kept as a
    # callback so this package doesn't import lifecycle.
    reconcile_group: Callable[[str], None]


@dataclass
class RetryContext:
    """One context object per retry pipeline run.

    Steps read and mutate this object in pipeline order; the executor
    is the only place that calls ``step.run(ctx)``.
    """

    # ------------------------------------------------------------------
    # Constructor inputs (immutable across pipeline run)
    # ------------------------------------------------------------------
    raw: dict[str, Any]
    error: str
    exclusions: AntiAffinityExclusions
    deps: RetryDeps

    # ------------------------------------------------------------------
    # Auto-retry flag — set by abort steps to silently short-circuit
    # downstream steps.  Manual retry never sets this; it raises instead.
    # ------------------------------------------------------------------
    aborted: bool = False

    # ------------------------------------------------------------------
    # State written by load/resolve steps
    # ------------------------------------------------------------------
    rj: RenderJob | None = None
    group_id: str = ""
    chunk_index: int = 0
    grp: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # State written by retry-decision steps
    # ------------------------------------------------------------------
    remaining: tuple[int, int, int] | None = None  # (frame_start, frame_end, frame_step)
    next_attempt: int = 0
    file_size_bytes: int | None = None
    engine: str | None = None
    tier: str | None = None

    # ------------------------------------------------------------------
    # State written by allocation/dispatch steps
    # ------------------------------------------------------------------
    chunk_request: AllocationChunkRequest | None = None
    # ``retry_task`` is now always None -- the retry pipeline parks the
    # request on pending_allocation_queue rather than synchronously
    # picking a target.  The daemon plans it on its next tick.  Field
    # kept for backwards-compat with any external reader; remove later.
    retry_task: PlannedTask | None = None
    # Set by ``ParkRetryToPendingStep`` -- means the chunk request was
    # parked for re-evaluation.  Downstream log + result steps read it.
    parked: bool = False
    dispatched: bool = False

    # ------------------------------------------------------------------
    # Manual-retry result payload (returned to API caller)
    # ------------------------------------------------------------------
    result: dict[str, Any] = field(default_factory=dict)
