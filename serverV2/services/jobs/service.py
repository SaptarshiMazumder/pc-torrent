"""JobService — standalone job lifecycle (upload, confirm, status, outputs, download).

Composes: job_repo, machine_repo, storage, upload_coordinator.
Pure DB operations — zero orchestration calls.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from typing import Any
from uuid import uuid4

from serverV2.core.value_objects import (
    MAX_UPLOAD_BYTES,
    SINGLE_PUT_MAX_BYTES,
    now_iso,
    parse_output_files,
    sanitize_filename,
)
from serverV2.infrastructure import storage
from serverV2.infrastructure.auth.firestore_client import write_job_record
from serverV2.services.jobs.serializers import build_output_entries, serialize_job

log = logging.getLogger(__name__)


class JobServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class JobService:

    def __init__(self, *, job_repo, machine_repo) -> None:
        self._jobs = job_repo
        self._machines = machine_repo

    # ---- upload request ----

    def request_upload(self, payload: Any, user: dict[str, Any]) -> dict[str, Any]:
        if not getattr(payload, "filename", None):
            raise JobServiceError(400, "filename is required")
        filename = sanitize_filename(payload.filename)
        machine_id = getattr(payload, "machine_id", None) or "unassigned"
        file_size_bytes = getattr(payload, "file_size_bytes", None)
        multipart_required = False
        if file_size_bytes is not None:
            from serverV2.services.upload.validators import validate_upload_size
            file_size_bytes = validate_upload_size(file_size_bytes)
            multipart_required = file_size_bytes > SINGLE_PUT_MAX_BYTES

        job_id = str(uuid4())
        r2_key = f"jobs/{job_id}/input/{filename}"
        upload_url = storage.generate_presigned_upload_url(r2_key)

        self._jobs.create_uploading_job(
            job_id=job_id,
            machine_id=machine_id,
            input_filename=filename,
            r2_key=r2_key,
            user_id=user["uid"],
        )

        return {
            "job_id": job_id,
            "upload_url": upload_url,
            "r2_key": r2_key,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "single_put_max_bytes": SINGLE_PUT_MAX_BYTES,
            "multipart_required": multipart_required,
            "file_size_bytes": file_size_bytes,
        }

    def confirm_upload(self, job_id: str, user: dict[str, Any]) -> dict[str, Any]:
        job = self._jobs.get_owned_by_id(job_id, user["uid"])
        if not job:
            raise JobServiceError(404, "Job not found")
        if job.get("status") != "uploading":
            raise JobServiceError(409, "Job is not in uploading state")
        self._jobs.confirm_upload(job_id)
        return {"job_id": job_id, "status": "pending"}

    # ---- status callbacks (from workers) ----

    _TERMINAL_STATUSES = frozenset({"cancelled", "done"})

    def update_status(self, job_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")

        current = str(job.get("status") or "")
        if current in self._TERMINAL_STATUSES:
            log.info("Rejecting status update %s→%s for job %s (terminal)", current, status, job_id)
            return {"job_id": job_id, "status": current, "success": False, "reason": "job already terminal"}

        self._jobs.update_status(job_id, status, error=error)
        return {"job_id": job_id, "status": status}

    def update_progress(self, job_id: str, rendered_frames: int, total_frames: int) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        self._jobs.update_progress(job_id, rendered_frames, total_frames)
        return {"job_id": job_id, "rendered_frames": rendered_frames, "total_frames": total_frames}

    def heartbeat(self, job_id: str, phase: str | None = None) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        if job.get("status") in ("cancelled", "done"):
            return {"job_id": job_id, "acknowledged": False, "reason": "job_cancelled"}
        if not self._jobs.update_heartbeat(job_id, phase):
            raise JobServiceError(404, "Job not found")
        return {"job_id": job_id, "acknowledged": True}

    # ---- output management ----

    def register_outputs(self, job_id: str, files: list[str]) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        merged = self._jobs.merge_output_files(job_id, files)
        return {"job_id": job_id, "output_files": merged}

    def get_output_entries(self, job_id: str) -> list[dict[str, Any]]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        return build_output_entries(job)

    def get_output_download_url(self, job_id: str, filename: str) -> str:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        key = f"jobs/{group_id}/output/{filename}"
        if not storage.file_exists(key):
            raise JobServiceError(404, "Output file not found")
        return storage.generate_presigned_url(key, download_name=filename)

    def download_all_as_zip(self, job_id: str) -> io.BytesIO:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        output_files = parse_output_files(job.get("output_files"))
        if not output_files:
            raise JobServiceError(404, "No output files available")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in output_files:
                key = f"jobs/{group_id}/output/{fname}"
                try:
                    data = storage.download_file(key)
                    zf.writestr(fname, data)
                except Exception as exc:
                    log.warning("Skipping %s in ZIP: %s", fname, exc)
        buf.seek(0)
        return buf

    # ---- next for machine (desktop agent polling) ----

    def next_for_machine(self, machine_id: str) -> dict[str, Any] | None:
        return self._jobs.claim_next_for_machine(machine_id)

    # ---- reads ----

    def get_status(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        from serverV2.infrastructure.db import query_one
        machine = query_one("SELECT * FROM machines WHERE id = %s", (job.get("machine_id"),))
        return serialize_job(job, machine)

    def list_by_user(self, user_id: str) -> list[dict[str, Any]]:
        return self._jobs.get_by_user(user_id)

    def delete(self, job_id: str, user: dict) -> dict[str, bool]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        if job.get("status") in ("pending", "running"):
            raise JobServiceError(400, "Cannot delete a job that is still in progress")
        self._jobs.delete_terminal(job_id)
        return {"success": True}
