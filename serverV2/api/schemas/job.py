from typing import Any, Literal
from pydantic import BaseModel


class RequestUploadPayload(BaseModel):
    machine_id: str | None = "unassigned"
    filename: str
    file_size_bytes: int | None = None


# Workers report liveness, failure, AND success.  Community workers
# explicitly self-mark "done" after their final upload to close the
# reclaim race -- if the agent restarts between "last register-outputs"
# and the success-notifier BackgroundTask firing, the next poll's
# self-heal would otherwise mark the row failed even though all frames
# uploaded successfully (handle_community_machine_idle).  Vast/Modal
# don't need this -- the orchestrator's per-job monitor + register-
# outputs counting still drive their success path.
class UpdateJobStatusPayload(BaseModel):
    status: Literal["running", "failed", "done"]
    error: str | None = None
    output_files: list[str] | None = None


class UpdateJobProgressPayload(BaseModel):
    total_frames: int | None = None
    rendered_frames: int


class JobHeartbeatPayload(BaseModel):
    # All optional -- backwards-compatible with workers that pre-date
    # the A3.2 telemetry fields.  Server's stall detector treats missing
    # fields as no-signal (skips the rule that needs them).
    phase: str | None = None
    cpu_percent: float | None = None
    rss_bytes: int | None = None
    bytes_progressed: int | None = None
    total_bytes: int | None = None


class UpdateJobTelemetryPayload(BaseModel):
    """Phase-11 data-richness payload pushed by the worker right before
    exit.  Stored on jobs.worker_telemetry_json; read by the orchestrator
    at completion time when it writes the render_telemetry row.

    Schemaless on purpose -- worker fields evolve faster than the server
    needs to keep up.  Known keys are documented in
    ``worker_core/telemetry_parser.py``.
    """
    telemetry: dict
