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
    phase: str | None = None
