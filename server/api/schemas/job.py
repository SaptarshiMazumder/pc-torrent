from typing import Any
from pydantic import BaseModel


class RequestUploadPayload(BaseModel):
    machine_id: str
    filename: str
    file_size_bytes: int | None = None


class UpdateJobStatusPayload(BaseModel):
    status: str
    error: str | None = None
    output_files: list[str] | None = None


class UpdateJobProgressPayload(BaseModel):
    total_frames: int | None = None
    rendered_frames: int


class UpdateJobHeartbeatPayload(BaseModel):
    phase: str
    detail: str | None = None


class MultipartInitPayload(BaseModel):
    file_size_bytes: int
    content_type: str | None = "application/octet-stream"
    part_size_bytes: int | None = None


class MultipartPartUrlsPayload(BaseModel):
    upload_id: str
    part_numbers: list[int]


class MultipartCompletePayload(BaseModel):
    upload_id: str
    parts: list[dict[str, Any]]


class MultipartAbortPayload(BaseModel):
    upload_id: str
