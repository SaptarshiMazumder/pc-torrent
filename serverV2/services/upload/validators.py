"""Upload validation — size limits, part size calculation. Pure functions."""

from __future__ import annotations

import math

from serverV2.core.value_objects import (
    MAX_UPLOAD_BYTES,
    MULTIPART_DEFAULT_PART_SIZE_BYTES,
    MULTIPART_MAX_PARTS,
    MULTIPART_MIN_PART_SIZE_BYTES,
)


class UploadValidationError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def validate_upload_size(total_bytes: int) -> int:
    if total_bytes <= 0:
        raise UploadValidationError(400, "file_size_bytes must be positive")
    if total_bytes > MAX_UPLOAD_BYTES:
        raise UploadValidationError(
            413, f"File too large ({total_bytes} bytes). Maximum is {MAX_UPLOAD_BYTES} bytes."
        )
    return total_bytes


def choose_part_size(file_size_bytes: int, requested: int | None = None) -> int:
    if requested and requested >= MULTIPART_MIN_PART_SIZE_BYTES:
        parts = math.ceil(file_size_bytes / requested)
        if parts <= MULTIPART_MAX_PARTS:
            return requested

    part_size = MULTIPART_DEFAULT_PART_SIZE_BYTES
    parts = math.ceil(file_size_bytes / part_size)
    if parts > MULTIPART_MAX_PARTS:
        part_size = math.ceil(file_size_bytes / MULTIPART_MAX_PARTS)
        part_size = max(part_size, MULTIPART_MIN_PART_SIZE_BYTES)

    return part_size


def compute_total_parts(file_size_bytes: int, part_size: int) -> int:
    return math.ceil(file_size_bytes / part_size)


def normalize_part_numbers(part_numbers: list[int]) -> list[int]:
    return sorted({int(n) for n in part_numbers if int(n) >= 1})


def normalize_completed_parts(parts: list[dict]) -> list[dict]:
    result = []
    for p in parts:
        pn = p.get("PartNumber") or p.get("part_number")
        etag = p.get("ETag") or p.get("etag")
        if pn is not None and etag:
            result.append({"PartNumber": int(pn), "ETag": str(etag)})
    return result
