"""JobService — worker-callback handling, output management, community pull.

Composes: job_repo, heartbeat_repo, progress_repo, worker_start_repo, outputs_resolver.
Pure DB operations — zero orchestration calls.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from serverV2.core.value_objects import parse_output_files, sanitize_filename
from serverV2.infrastructure import storage
from serverV2.services.jobs.outputs_resolver import OutputsResolver

log = logging.getLogger(__name__)


class JobServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class JobService:

    def __init__(
        self,
        *,
        job_repo,
        machine_repo,
        heartbeat_repo,
        progress_repo,
        worker_start_repo,
        outputs_resolver: OutputsResolver,
    ) -> None:
        self._jobs = job_repo
        self._machines = machine_repo
        self._heartbeats = heartbeat_repo
        self._progress = progress_repo
        self._worker_start = worker_start_repo
        self._outputs = outputs_resolver

    # ---- duplicate-start guard for serverless containers ----

    def try_claim_worker_start(self, job_id: str) -> bool:
        """Called once per container start.  True if this caller is the
        first to claim, False if another container already claimed this
        ``job_id`` — the worker must abort to defeat Modal's re-queue.
        """
        return self._worker_start.try_claim(job_id)

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
        self._progress.record(job_id, rendered_frames, total_frames)
        return {"job_id": job_id, "rendered_frames": rendered_frames, "total_frames": total_frames}

    def heartbeat(self, job_id: str, phase: str | None = None) -> dict[str, Any]:
        self._heartbeats.record(job_id, phase)
        return {"job_id": job_id, "acknowledged": True}

    # ---- output management ----

    def register_outputs(self, job_id: str, files: list[str]) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        merged = self._jobs.merge_output_files(job_id, files)
        return {"job_id": job_id, "output_files": merged}

    def get_outputs(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        scope_id = job.get("group_id") or job_id
        entries = self._outputs.entries([job], scope_id=scope_id)
        return {"job_id": job_id, "files": entries, "count": len(entries)}

    def get_output_download_url(self, job_id: str, filename: str) -> str:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        key = f"jobs/{group_id}/output/{filename}"
        if not storage.file_exists(key):
            raise JobServiceError(404, "Output file not found")
        return storage.generate_presigned_url(key, download_name=filename)

    def get_output_preview(self, job_id: str, filename: str) -> tuple[bytes, str]:
        from serverV2.services.previews.renderer import (
            PREVIEW_MEDIA_TYPE,
            PreviewRenderError,
            generate_image_preview,
        )
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        key = f"jobs/{group_id}/output/{sanitize_filename(filename)}"
        if not storage.file_exists(key):
            raise JobServiceError(404, "Output file not found")
        try:
            raw = storage.download_file(key)
        except Exception as exc:
            raise JobServiceError(404, "Output file not found") from exc
        try:
            return generate_image_preview(raw), PREVIEW_MEDIA_TYPE
        except PreviewRenderError as exc:
            raise JobServiceError(415, str(exc)) from exc

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

