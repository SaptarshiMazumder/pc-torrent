from typing import Any, Literal
from pydantic import BaseModel


class RequestUploadPayload(BaseModel):
    machine_id: str | None = "unassigned"
    filename: str
    file_size_bytes: int | None = None


# Workers may only report liveness and failure.  "done" is orchestrator-owned
# and determined by verified output_files count, not worker self-report.
class UpdateJobStatusPayload(BaseModel):
    status: Literal["running", "failed"]
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
