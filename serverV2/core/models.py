"""Domain models — immutable dataclasses with zero I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from serverV2.core.enums import JobStatus, SERVERLESS_TYPE_VALUES

if TYPE_CHECKING:
    from serverV2.config import StartupBufferConfig


# ---------------------------------------------------------------------------
# AllocationTarget -- structural typing seam for the allocation planner.
#
# Both ``CommunityMachine`` and ``FleetCapability`` satisfy this Protocol.
# The planner scores and filters against this surface, dropping its
# previous ``isinstance(target, X)`` discrimination.  Cardinality and
# dispatch routing stay outside the Protocol -- those remain genuinely
# different and AllocationDispatcher still pattern-matches on the
# concrete type.
#
# Step 1 (this commit): Protocol declared, no consumers yet.  Pure
# additive change.  Subsequent steps add the @property implementations
# on the two dataclasses, then migrate the planner.
# ---------------------------------------------------------------------------
@runtime_checkable
class AllocationTarget(Protocol):
    # ---- shared scoring / filter surface --------------------------------
    vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float
    price_per_hour: float
    available_seconds: float | None
    cuda_version: str | None
    host_os: str | None

    # ---- discrimination via attribute access ----------------------------
    # Replaces the planner's six module-level isinstance helpers.
    #
    # ``fleet_key``           "community" / fleet name -- diversification bucket.
    # ``gpu_type_key``        "" for community / "<fleet>:<gpu_type>" else.
    # ``dispatch_fleet``      Fleet identifier for startup-buffer + telemetry.
    @property
    def fleet_key(self) -> str: ...

    @property
    def gpu_type_key(self) -> str: ...

    @property
    def dispatch_fleet(self) -> str | None: ...

    # ``machine_id`` and ``serverless_capability_key`` are mutually
    # exclusive views over the target's identity, used by the retry
    # exclusion filter and the community-first dedup pass.
    #   - CommunityMachine: machine_id = self.id, capability_key = None
    #   - FleetCapability:  machine_id = None,    capability_key = (fleet, gpu_type)
    @property
    def machine_id(self) -> str | None: ...

    @property
    def serverless_capability_key(self) -> tuple[str, str] | None: ...

    # ---- PlannedTask construction ---------------------------------------
    # Each concrete type knows how to build its own PlannedTask shape
    # (community sets machine_id; serverless sets gpu_type + offer_id).
    # The planner computes the time/cost estimates once via its analyzers
    # and passes them in; the target fills in routing-specific fields.
    def to_planned_task(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        chunk_index: int | None,
        attempt: int,
        estimated_seconds: float,
        estimated_cost_usd: float,
        estimated_seconds_per_frame: float,
        estimated_startup_seconds: float,
    ) -> "PlannedTask": ...


# ---------------------------------------------------------------------------
# CommunityMachine — a real desktop with stable identity owned by a user.
# Persisted as a row in the `machines` table.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommunityMachine:
    id: str
    gpu_model: str
    vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float
    status: str
    last_seen_at: str | None
    # Cost-aware allocators (Phase 5+) read this; today no per-machine price
    # column exists in the ``machines`` table — every community machine gets
    # the global ``community.price_per_hour`` injected by MachineRepository.
    price_per_hour: float = 1.0
    # Seconds remaining in the host's commitment window, computed at row-read
    # time from ``commitment_end_at - now()``.  None when the column is unset
    # (pre-migration rows) -- planner's time filter treats None as "skip the
    # check" so the planner stays compatible with legacy rows.  Phase 5 reads
    # this directly; Phase 1 just plumbs the slot.
    available_seconds: float | None = None

    # ---- AllocationTarget Protocol -----------------------------------------
    # Constant slots: community has no driver CUDA / host OS info today.
    # Kept as @property (not fields) so the dataclass field surface stays
    # unchanged.
    @property
    def cuda_version(self) -> str | None:
        return None

    @property
    def host_os(self) -> str | None:
        return None

    @property
    def fleet_key(self) -> str:
        return "community"

    @property
    def gpu_type_key(self) -> str:
        # Community machines are unique by ``id`` already -- no per-gpu-type
        # diversification cap applies to them.  Empty key signals
        # "skip me from the cap counter" to the diversification step.
        return ""

    @property
    def dispatch_fleet(self) -> str | None:
        return "community"

    @property
    def machine_id(self) -> str | None:
        return self.id

    @property
    def serverless_capability_key(self) -> tuple[str, str] | None:
        return None

    def to_planned_task(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        chunk_index: int | None,
        attempt: int,
        estimated_seconds: float,
        estimated_cost_usd: float,
        estimated_seconds_per_frame: float,
        estimated_startup_seconds: float,
    ) -> "PlannedTask":
        return PlannedTask(
            fleet="community",
            machine_id=self.id,
            gpu_type=None,
            label=self.gpu_model,
            vram_gb=self.vram_gb,
            render_speed=self.render_speed,
            price_per_hour=self.price_per_hour,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
            estimated_seconds=estimated_seconds,
            estimated_cost_usd=estimated_cost_usd,
            estimated_seconds_per_frame=estimated_seconds_per_frame,
            estimated_startup_seconds=estimated_startup_seconds,
        )

    @classmethod
    def from_row(cls, row: dict[str, Any], *, price_per_hour: float = 1.0) -> CommunityMachine:
        return cls(
            id=row["id"],
            gpu_model=row.get("gpu_model", "Unknown"),
            vram_gb=row.get("gpu_vram_gb") or 0,
            cpu_cores=row.get("cpu_cores") or 0,
            ram_gb=row.get("ram_gb") or 0,
            render_speed=row.get("render_speed") or 1.0,
            status=row.get("status", "idle"),
            last_seen_at=row.get("last_seen_at"),
            price_per_hour=price_per_hour,
            available_seconds=_compute_available_seconds(row.get("commitment_end_at")),
        )


def _compute_available_seconds(commitment_end_at: Any) -> float | None:
    """Convert a ``commitment_end_at`` cell into seconds-remaining.

    Returns ``None`` if the column is unset (legacy / unmigrated row).
    Returns ``0`` for a window that has already expired -- planner will
    drop the row, the community monitor will retire it on its next tick.
    """
    if commitment_end_at is None:
        return None
    if isinstance(commitment_end_at, datetime):
        end_at = commitment_end_at
    else:
        # ISO string fallback for DB layers that don't auto-cast TIMESTAMP.
        try:
            end_at = datetime.fromisoformat(str(commitment_end_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    remaining = (end_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, remaining)


# ---------------------------------------------------------------------------
# FleetCapability — "we can provision N of this kind of serverless machine."
# Built at runtime from config.json + live in-flight count; never persisted.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FleetCapability:
    fleet: str               # "modal_serverless" | "vast_serverless"
    gpu_type: str            # "h100", "rtx_a6000", etc. — used for routing
    label: str
    vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float
    fleet_max_parallel: int
    # Per-hour rental cost.  For Vast this is the offer's actual dph_total
    # (per-offer marketplace price); for Modal it's the config-fixed price.
    price_per_hour: float = 0.0
    # Vast-only: the rentable bundle id for this exact offer.  None for
    # Modal/community.  When set, the dispatch path rents THIS offer
    # rather than re-searching by gpu_type.
    offer_id: int | None = None
    # Driver-reported CUDA version (e.g. "12.8").  Vast surfaces this per
    # offer; Modal/community leave it None.  Scorer's "None means trust"
    # rule gives None full credit instead of penalising it.
    cuda_version: str | None = None
    # Host OS string (e.g. "Linux", "Ubuntu 22.04", "Windows Server").
    # Same "None means trust" rule applies.
    host_os: str | None = None
    # Seconds the underlying resource is committed for.  Modal stamps the
    # config-driven ``availability_sec``; Vast stamps the offer's
    # ``duration`` (or ``end_date - now()`` as a fallback).  ``None`` is a
    # real semantic -- "the source didn't tell us" -- and the planner
    # respects that by skipping its time check for that target.
    available_seconds: float | None = None

    # ---- AllocationTarget Protocol -----------------------------------------
    @property
    def fleet_key(self) -> str:
        return self.fleet

    @property
    def gpu_type_key(self) -> str:
        return f"{self.fleet}:{self.gpu_type}"

    @property
    def dispatch_fleet(self) -> str | None:
        return self.fleet

    @property
    def machine_id(self) -> str | None:
        return None

    @property
    def serverless_capability_key(self) -> tuple[str, str] | None:
        return (self.fleet, self.gpu_type)

    def to_planned_task(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        chunk_index: int | None,
        attempt: int,
        estimated_seconds: float,
        estimated_cost_usd: float,
        estimated_seconds_per_frame: float,
        estimated_startup_seconds: float,
    ) -> "PlannedTask":
        return PlannedTask(
            fleet=self.fleet,
            machine_id=None,
            gpu_type=self.gpu_type,
            label=self.label,
            vram_gb=self.vram_gb,
            render_speed=self.render_speed,
            price_per_hour=self.price_per_hour,
            offer_id=self.offer_id,
            cuda_version=self.cuda_version,
            host_os=self.host_os,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            chunk_index=chunk_index,
            attempt=attempt,
            estimated_seconds=estimated_seconds,
            estimated_cost_usd=estimated_cost_usd,
            estimated_seconds_per_frame=estimated_seconds_per_frame,
            estimated_startup_seconds=estimated_startup_seconds,
        )


# ---------------------------------------------------------------------------
# AvailableResources — what the allocator is given to plan against.
# Built per-allocation by RenderLifecycle._resource_picker.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AvailableResources:
    community_machines: list[CommunityMachine]
    serverless_capabilities: list[FleetCapability]
    serverless_in_flight: dict[str, int]   # fleet -> count of pending+running


# ---------------------------------------------------------------------------
# Render Job
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderJob:
    job_id: str
    group_id: str
    machine_id: str
    machine_type: str
    status: str
    frame_start: int
    frame_end: int
    frame_step: int
    rendered_frames: int
    total_frames: int
    attempt: int
    max_retries: int
    submitted_at: str
    last_heartbeat_at: str | None
    input_filename: str
    render_overrides_json: str | None
    chunk_index: int | None
    priority: int
    # Planner-stamped startup budget; resolver uses this at dispatch
    # to compute the loading-stall deadline.
    estimated_startup_seconds: float = 0.0
    # Resolved kill-time deadlines stamped at dispatch.  Read by the
    # fleet singletons (and CommunityMonitor) per-tick to enforce
    # stall checks.  None for legacy rows pre-dating the column.
    allowed_stall_times: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RenderJob:
        return cls(
            job_id=row["id"],
            group_id=row.get("group_id", ""),
            machine_id=row.get("machine_id", ""),
            machine_type=row.get("machine_type") or "windows",
            status=row.get("status", "pending"),
            frame_start=row.get("frame_start") or 0,
            frame_end=row.get("frame_end") or 0,
            frame_step=row.get("frame_step") or 1,
            rendered_frames=max(0, row.get("rendered_frames") or 0),
            total_frames=row.get("total_frames") or 0,
            attempt=row.get("attempt") or 0,
            max_retries=row.get("max_retries") or 0,
            submitted_at=row.get("submitted_at") or "",
            last_heartbeat_at=row.get("last_heartbeat_at"),
            input_filename=row.get("input_filename", ""),
            render_overrides_json=row.get("render_overrides_json"),
            chunk_index=row.get("chunk_index"),
            priority=row.get("priority") or 0,
            estimated_startup_seconds=float(row.get("estimated_startup_seconds") or 0.0),
            allowed_stall_times=row.get("allowed_stall_times"),
        )

    @property
    def is_terminal(self) -> bool:
        return JobStatus(self.status).is_terminal

    @property
    def is_serverless(self) -> bool:
        return self.machine_type in SERVERLESS_TYPE_VALUES

    def can_retry_same_endpoint(self) -> bool:
        return self.attempt < self.max_retries

    def is_complete_by_frames(self) -> bool:
        return self.total_frames > 0 and self.rendered_frames >= self.total_frames

    def is_heartbeat_dead(self, grace_sec: float, timeout_sec: float) -> bool:
        if not self.submitted_at:
            return False
        now = datetime.now(timezone.utc)
        try:
            submitted = datetime.fromisoformat(self.submitted_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        if (now - submitted).total_seconds() < grace_sec:
            return False
        if self.last_heartbeat_at is None:
            return True
        try:
            hb_time = datetime.fromisoformat(str(self.last_heartbeat_at).replace("Z", "+00:00"))
            return (now - hb_time).total_seconds() > timeout_sec
        except (ValueError, TypeError):
            return False


# ---------------------------------------------------------------------------
# Planned Task (output of frame allocation).
#
# `fleet` is the discriminant.  For community tasks `machine_id` is set
# and `gpu_type` is None.  For serverless tasks `gpu_type` is set and
# `machine_id` is None — the strategy provisions a fresh container at
# dispatch time.  The dispatcher routes purely by `fleet`.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedTask:
    fleet: str
    label: str
    vram_gb: float
    render_speed: float
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    machine_id: str | None = None     # community fleet only
    gpu_type: str | None = None       # serverless fleets only
    chunk_index: int | None = None
    attempt: int = 0
    # Per-hour rental snapshot, copied from the target (FleetCapability or
    # CommunityMachine) at allocation time.  Phase 9 cost preview reads
    # this to build MixSlots without re-looking-up prices.
    price_per_hour: float = 0.0
    # Vast-only: the exact offer chosen at planning time.  Carried through
    # the dispatch_queue so the dispatcher rents THIS offer (not a
    # re-search match).  None for Modal/community.
    offer_id: int | None = None
    # Per-offer CUDA / OS metadata captured at planning time so the
    # dispatch handler can log / telemetry without re-querying Vast.
    cuda_version: str | None = None
    host_os: str | None = None
    # Per-chunk estimates stamped by ``AllocationPlanner`` at planning
    # time.  Persisted onto the ``jobs`` row at dispatch so the cost
    # service can sum them across a group for "estimated total" and
    # project mid-render totals.  All in seconds / USD.
    estimated_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    estimated_seconds_per_frame: float = 0.0
    estimated_startup_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Dispatch Context & Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchContext:
    group_id: str
    input_filename: str
    render_overrides_json: str
    blend_url: str
    max_retries: int = 0
    priority: int = 0
    # Render engine ("BLENDER_EEVEE", "CYCLES", ...).  Read by fleet
    # strategies that need to vary infrastructure by engine — currently
    # VastFleetStrategy uses it to pick the eevee-vs-cycles docker image.
    engine: str | None = None


@dataclass(frozen=True)
class DispatchResult:
    job_id: str
    machine_id: str
    status: str
    provider_job_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Group status aggregation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GroupStatusResult:
    status: str
    should_persist: bool


# ---------------------------------------------------------------------------
# Create-job params (value object for repository)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateJobParams:
    job_id: str
    fleet: str                       # "community" | "modal_serverless" | "vast_serverless"
    group_id: str
    input_filename: str
    total_frames: int
    frame_start: int
    frame_end: int
    frame_step: int
    render_overrides_json: str
    machine_id: str | None = None    # community only
    gpu_type: str | None = None      # serverless only
    max_retries: int = 0
    priority: int = 0
    chunk_index: int | None = None
    attempt: int = 0
    # Per-hour rental snapshot at dispatch time.  Looked up by the fleet
    # strategy from its config and stamped here so the SuccessHandler's
    # telemetry write is immune to later config edits.  None for community
    # jobs (telemetry is skipped for them in v1).
    price_per_hour_at_dispatch: float | None = None
    # Per-chunk estimates produced by ``AllocationPlanner`` and persisted
    # onto the ``jobs`` row.  Read by the cost service for total / live
    # projection sums.
    estimated_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    estimated_seconds_per_frame: float = 0.0
    estimated_startup_seconds: float = 0.0
    # Resolved kill-time deadlines for this chunk, computed at dispatch
    # by ``AllowedStallTimesResolver``.  Written to the
    # ``jobs.allowed_stall_times`` JSONB column and read by:
    #   * fleet singletons each tick (stall enforcement)
    #   * UI on instance card mount (deadline display)
    # Single source of truth for the clamp/multiplier math.
    allowed_stall_times: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Render Group
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderGroup:
    id: str
    input_filename: str
    r2_input_key: str
    total_frames: int
    frame_start: int
    frame_end: int
    frame_step: int
    status: str
    submitted_at: str
    user_id: str | None = None
    source_asset_id: str | None = None
    render_overrides_json: str | None = None
    scheduling_json: str | None = None
    analysis_snapshot_json: str | None = None
    analysis_warnings_json: str | None = None
    completed_at: str | None = None
    error: str | None = None
    allowed_machine_types_json: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RenderGroup:
        return cls(
            id=row["id"],
            input_filename=row.get("input_filename", ""),
            r2_input_key=row.get("r2_input_key", ""),
            total_frames=row.get("total_frames") or 0,
            frame_start=row.get("frame_start") or 1,
            frame_end=row.get("frame_end") or 1,
            frame_step=row.get("frame_step") or 1,
            status=row.get("status", "uploading"),
            submitted_at=row.get("submitted_at") or "",
            user_id=row.get("user_id"),
            source_asset_id=row.get("source_asset_id"),
            render_overrides_json=row.get("render_overrides_json"),
            scheduling_json=row.get("scheduling_json"),
            analysis_snapshot_json=row.get("analysis_snapshot_json"),
            analysis_warnings_json=row.get("analysis_warnings_json"),
            completed_at=row.get("completed_at"),
            error=row.get("error"),
            allowed_machine_types_json=row.get("allowed_machine_types_json"),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "failed", "cancelled")


# ---------------------------------------------------------------------------
# User Input File
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UserInputFile:
    id: str
    user_id: str
    display_name: str
    input_filename: str
    r2_key: str
    frame_start: int | None = None
    frame_end: int | None = None
    frame_step: int | None = None
    analysis_snapshot_json: str | None = None
    render_overrides_json: str | None = None
    scheduling_json: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_used_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> UserInputFile:
        return cls(
            id=row["id"],
            user_id=row.get("user_id", ""),
            display_name=row.get("display_name") or row.get("input_filename", ""),
            input_filename=row.get("input_filename", ""),
            r2_key=row.get("r2_key", ""),
            frame_start=row.get("frame_start"),
            frame_end=row.get("frame_end"),
            frame_step=row.get("frame_step"),
            analysis_snapshot_json=row.get("analysis_snapshot_json"),
            render_overrides_json=row.get("render_overrides_json"),
            scheduling_json=row.get("scheduling_json"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            last_used_at=row.get("last_used_at"),
        )


# ---------------------------------------------------------------------------
# Frame plan result (output of frame planning)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FramePlanResult:
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int


# ---------------------------------------------------------------------------
# Instance Snapshot (live status from fleet monitors)
# ---------------------------------------------------------------------------

@dataclass
class InstanceSnapshot:
    job_id: str
    fleet_type: str
    provider_status: str = ""
    gpu_label: str = ""
    rendered_frames: int = 0
    total_frames: int = 0
    elapsed_sec: float | None = None
    cost: str | None = None
    error: str | None = None
    logs: str | None = None
    status_history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "fleet_type": self.fleet_type,
            "provider_status": self.provider_status,
            "gpu_label": self.gpu_label,
            "rendered_frames": self.rendered_frames,
            "total_frames": self.total_frames,
            "elapsed_sec": self.elapsed_sec,
            "cost": self.cost,
            "error": self.error,
            "logs": self.logs,
            "status_history": self.status_history,
        }
