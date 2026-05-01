"""Domain models — immutable dataclasses with zero I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from serverV2.core.enums import JobStatus, SERVERLESS_TYPE_VALUES


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
        )


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
    # Per-hour rental cost.  Read by cost-aware allocators (Phase 5+).
    # Wired in bootstrap from VastEndpoint/ModalEndpoint.price_per_hour.
    price_per_hour: float = 0.0


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
