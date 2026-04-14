from typing import Any
from pydantic import BaseModel


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
