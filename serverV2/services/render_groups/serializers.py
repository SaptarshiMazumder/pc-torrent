"""Serializers — pure formatting helpers for render group responses."""

from __future__ import annotations

from typing import Any

from serverV2.core.models import RenderJob
from serverV2.core.value_objects import (
    compute_progress_pct,
    latest_output_filename,
    parse_output_files,
)


def serialize_task(
    job: dict[str, Any],
    machine: dict[str, Any] | None = None,
    *,
    is_retryable: bool = False,
) -> dict[str, Any]:
    total_frames = job.get("total_frames")
    output_files = parse_output_files(job.get("output_files"))

    # Verified upload count is the single source of truth for progress.
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
    remaining_start: int | None = None
    remaining_end: int | None = None
    remaining = RenderJob.from_row(job).remaining_frames()
    if remaining is not None:
        remaining_start, remaining_end = remaining

    return {
        "job_id": job["id"],
        "machine_id": job["machine_id"],
        "machine_type": fleet or (machine.get("machine_type", "windows") if machine else "windows"),
        "machine_gpu": display_gpu,
        "machine_vram": display_vram,
        "frame_start": job.get("frame_start"),
        "frame_end": job.get("frame_end"),
        "frame_step": job.get("frame_step") or 1,
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
        "latest_output_file": latest_output_filename(output_files),
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
