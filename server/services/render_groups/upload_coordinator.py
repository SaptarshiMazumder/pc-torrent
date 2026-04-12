"""
Upload coordinator — multipart + single-PUT flow for render groups.

No HTTP dependencies: raises ``UploadError`` that the router maps to
``HTTPException``.  All storage I/O goes through ``infrastructure.storage``.
"""

from __future__ import annotations

import logging
from typing import Any

import infrastructure.storage as storage
from api.routers.jobs import (
    _choose_multipart_part_size,
    _compute_total_parts,
    _normalize_completed_parts,
    _normalize_part_numbers,
    _validate_upload_size,
)
from models.value_objects import MAX_UPLOAD_BYTES, SINGLE_PUT_MAX_BYTES
from infrastructure.db import query_one

log = logging.getLogger(__name__)


class UploadError(Exception):
    """Raised when an upload operation fails.

    ``status`` is an HTTP-friendly status code that the router can forward
    directly to ``HTTPException``.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _load_group(group_id: str, user_id: str, columns: str = "id, r2_input_key, status, user_id") -> dict[str, Any]:
    group = query_one(
        f"SELECT {columns} FROM render_groups WHERE id = %s", (group_id,)
    )
    if not group:
        raise UploadError(404, "Render group not found")
    if not group.get("user_id") or group["user_id"] != user_id:
        raise UploadError(403, "Access denied")
    return group


class UploadCoordinator:

    def init_multipart(
        self,
        group_id: str,
        user_id: str,
        total_bytes: int,
        content_type: str | None = None,
        part_size_bytes: int | None = None,
    ) -> dict[str, Any]:
        group = _load_group(group_id, user_id)
        if group["status"] != "uploading":
            raise UploadError(409, "Render group is not in uploading state")

        file_size_bytes = _validate_upload_size(total_bytes)
        chosen_part_size = _choose_multipart_part_size(file_size_bytes, part_size_bytes)
        total_parts = _compute_total_parts(file_size_bytes, chosen_part_size)
        r2_key = group["r2_input_key"]

        try:
            upload_id = storage.create_multipart_upload(
                r2_key, content_type=(content_type or "application/octet-stream")
            )
        except Exception as exc:
            log.error("Failed to create multipart upload for render group %s: %s", group_id, exc)
            raise UploadError(500, "Failed to initialize multipart upload")

        return {
            "upload_id": upload_id,
            "part_size_bytes": chosen_part_size,
            "total_parts": total_parts,
            "file_size_bytes": file_size_bytes,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "single_put_max_bytes": SINGLE_PUT_MAX_BYTES,
            "r2_key": r2_key,
        }

    def part_urls(
        self,
        group_id: str,
        user_id: str,
        upload_id: str,
        part_numbers: list[int],
    ) -> dict[str, Any]:
        group = _load_group(group_id, user_id)
        if group["status"] != "uploading":
            raise UploadError(409, "Render group is not in uploading state")

        r2_key = group["r2_input_key"]
        numbers = _normalize_part_numbers(part_numbers)
        urls = {
            str(n): storage.generate_presigned_upload_part_url(r2_key, upload_id, n)
            for n in numbers
        }
        return {"upload_id": upload_id, "urls": urls}

    def complete(
        self,
        group_id: str,
        user_id: str,
        upload_id: str,
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        group = _load_group(group_id, user_id)
        if group["status"] != "uploading":
            raise UploadError(409, "Render group is not in uploading state")

        normalized = _normalize_completed_parts(parts)
        try:
            result = storage.complete_multipart_upload(group["r2_input_key"], upload_id, normalized)
        except Exception as exc:
            log.error("Failed to complete multipart upload for render group %s: %s", group_id, exc)
            raise UploadError(400, f"Failed to complete multipart upload: {exc}")

        return {
            "success": True,
            "upload_id": upload_id,
            "etag": result.get("ETag"),
            "location": result.get("Location"),
            "key": result.get("Key"),
        }

    def abort(
        self,
        group_id: str,
        user_id: str,
        upload_id: str,
    ) -> dict[str, Any]:
        group = query_one(
            "SELECT id, r2_input_key, user_id FROM render_groups WHERE id = %s", (group_id,)
        )
        if not group:
            raise UploadError(404, "Render group not found")
        if not group.get("user_id") or group["user_id"] != user_id:
            raise UploadError(403, "Access denied")
        try:
            storage.abort_multipart_upload(group["r2_input_key"], upload_id)
        except Exception as exc:
            raise UploadError(400, f"Failed to abort multipart upload: {exc}")
        return {"success": True, "upload_id": upload_id}
