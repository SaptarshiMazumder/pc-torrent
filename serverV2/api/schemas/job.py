from typing import Any
from pydantic import BaseModel


class RequestUploadPayload(BaseModel):
    machine_id: str | None = "unassigned"
    filename: str
    file_size_bytes: int | None = None


class UpdateJobStatusPayload(BaseModel):
    status: str
    error: str | None = None
    output_files: list[str] | None = None


class UpdateJobProgressPayload(BaseModel):
    total_frames: int | None = None
    rendered_frames: int


class JobHeartbeatPayload(BaseModel):
    phase: str | None = None
