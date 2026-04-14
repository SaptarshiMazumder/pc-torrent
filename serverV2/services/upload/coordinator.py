"""UploadCoordinator — orchestrates multipart upload lifecycle.

Composes: storage infrastructure + validators. No DB access (caller provides group).
"""

from __future__ import annotations

import logging
from typing import Any

from serverV2.infrastructure import storage
from serverV2.services.upload.validators import (
    UploadValidationError,
    choose_part_size,
    compute_total_parts,
    normalize_completed_parts,
    normalize_part_numbers,
    validate_upload_size,
)
from serverV2.core.value_objects import MAX_UPLOAD_BYTES, SINGLE_PUT_MAX_BYTES

log = logging.getLogger(__name__)


class UploadCoordinator:

    def init_multipart(
        self,
        r2_key: str,
        total_bytes: int,
        content_type: str | None = None,
        part_size_bytes: int | None = None,
    ) -> dict[str, Any]:
        file_size = validate_upload_size(total_bytes)
        chosen_part_size = choose_part_size(file_size, part_size_bytes)
        total_parts = compute_total_parts(file_size, chosen_part_size)

        upload_id = storage.create_multipart_upload(
            r2_key, content_type=(content_type or "application/octet-stream"),
        )

        return {
            "upload_id": upload_id,
            "part_size_bytes": chosen_part_size,
            "total_parts": total_parts,
            "file_size_bytes": file_size,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "single_put_max_bytes": SINGLE_PUT_MAX_BYTES,
            "r2_key": r2_key,
        }

    def part_urls(
        self,
        r2_key: str,
        upload_id: str,
        part_numbers: list[int],
    ) -> dict[str, Any]:
        numbers = normalize_part_numbers(part_numbers)
        urls = {
            str(n): storage.generate_presigned_upload_part_url(r2_key, upload_id, n)
            for n in numbers
        }
        return {"upload_id": upload_id, "urls": urls}

    def complete(
        self,
        r2_key: str,
        upload_id: str,
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized = normalize_completed_parts(parts)
        result = storage.complete_multipart_upload(r2_key, upload_id, normalized)
        return {
            "success": True,
            "upload_id": upload_id,
            "etag": result.get("ETag"),
            "location": result.get("Location"),
            "key": result.get("Key"),
        }

    def abort(self, r2_key: str, upload_id: str) -> dict[str, Any]:
        storage.abort_multipart_upload(r2_key, upload_id)
        return {"success": True, "upload_id": upload_id}
