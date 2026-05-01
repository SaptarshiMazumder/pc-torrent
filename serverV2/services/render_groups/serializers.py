"""Serializers — formatting helpers for render group responses.

``RenderGroupSerializer`` is now a class (was a pure module) so it can
hold an ``OutputFrameRepository`` reference.  Per-task fields that used
to read the legacy ``jobs.output_files`` JSON column now read from the
``output_frames`` table directly via the repo.

``build_dispatch_task_entry`` stays a module-level pure function — it
formats a freshly-planned task that has no DB-side state yet.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.value_objects import (
    compute_progress_pct,
    output_frame_sort_key,
)
from serverV2.repositories.output_frame_repository import OutputFrameRepository


class RenderGroupSerializer:

    def __init__(self, *, output_frame_repo: OutputFrameRepository) -> None:
        self._output_frames = output_frame_repo

    def serialize_task(
        self,
        job: dict[str, Any],
        machine: dict[str, Any] | None = None,
        *,
        is_retryable: bool = False,
    ) -> dict[str, Any]:
        total_frames = job.get("total_frames")
        # Verified-upload list is the single source of truth for progress.
        # Pull straight from output_frames; sibling-retry duplicates are
        # already collapsed at INSERT time (PK on (group_id, filename)).
        output_files = self._output_frames.list_for_job(job["id"])

        rendered_frames = len(output_files)
        if total_frames and total_frames > 0:
            rendered_frames = min(rendered_frames, total_frames)

        fleet = (job.get("machine_type") or "").strip()
        gpu_type = (job.get("gpu_type") or "").strip()
        actual_gpu = job.get("actual_gpu_name")
        actual_vram = job.get("actual_gpu_vram_gb")

        if fleet == "vast_serverless":
            if actual_gpu:
                vram_suffix = f" {int(actual_vram)}GB" if actual_vram else ""
                display_gpu = f"Vast {actual_gpu}{vram_suffix}"
                display_vram = actual_vram or 0
            else:
                display_gpu = f"Vast {gpu_type}" if gpu_type else "Vast"
                display_vram = actual_vram or 0
        elif fleet == "modal_serverless":
            display_gpu = f"Modal {gpu_type.upper()}" if gpu_type else "Modal"
            display_vram = actual_vram or 0
        elif machine:
            display_gpu = machine.get("gpu_model") or "Unknown"
            display_vram = machine.get("gpu_vram_gb", 0)
        else:
            display_gpu = "Unknown"
            display_vram = 0

        # Verified-upload-based gap.  Frontend uses these to display the
        # actual missing range on stuck chunks instead of the original
        # frame_start/end (which double-counts already-uploaded frames).
        # Workers render sequentially, so uploaded count → start offset.
        remaining_start: int | None = None
        remaining_end: int | None = None
        frame_start = job.get("frame_start") or 0
        frame_end = job.get("frame_end") or 0
        frame_step = job.get("frame_step") or 1
        new_start = frame_start + len(output_files) * frame_step
        if new_start <= frame_end:
            remaining_start, remaining_end = new_start, frame_end

        latest = max(output_files, key=output_frame_sort_key) if output_files else None

        return {
            "job_id": job["id"],
            "machine_id": job["machine_id"],
            "machine_type": fleet or (machine.get("machine_type", "windows") if machine else "windows"),
            "machine_gpu": display_gpu,
            "machine_vram": display_vram,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "chunk_index": job.get("chunk_index"),
            "total_frames": total_frames,
            "rendered_frames": rendered_frames,
            "progress_pct": compute_progress_pct(
                job.get("status", ""), rendered_frames, total_frames,
            ),
            "status": job.get("status", "pending"),
            "error": job.get("error"),
            "attempt": job.get("attempt") or 0,
            "max_retries": job.get("max_retries") or 0,
            "priority": job.get("priority") or 0,
            "output_files_count": len(output_files),
            "latest_output_file": latest,
            "remaining_frame_start": remaining_start,
            "remaining_frame_end": remaining_end,
            "is_retryable": is_retryable,
        }


def build_dispatch_task_entry(
    planned_task: Any,
    dispatch_result: Any,
    scheduling: dict[str, Any],
) -> dict[str, Any]:
    return {
        "job_id": dispatch_result.job_id,
        "machine_id": planned_task.machine_id,
        "machine_gpu": planned_task.label,
        "machine_vram": planned_task.vram_gb,
        "frame_start": planned_task.frame_start,
        "frame_end": planned_task.frame_end,
        "frame_step": planned_task.frame_step,
        "chunk_index": planned_task.chunk_index,
        "total_frames": planned_task.total_frames,
        "rendered_frames": 0,
        "progress_pct": None,
        "status": "pending",
        "error": None,
        "attempt": 0,
        "max_retries": scheduling.get("max_retries_per_chunk", 0),
        "priority": scheduling.get("priority", 0),
    }
