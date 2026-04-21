"""Serializers — pure formatting helpers for render group responses."""

from __future__ import annotations

from typing import Any

from serverV2.core.value_objects import (
    compute_progress_pct,
    latest_output_filename,
    parse_output_files,
)


def serialize_task(job: dict[str, Any], machine: dict[str, Any] | None = None) -> dict[str, Any]:
    total_frames = job.get("total_frames")
    output_files = parse_output_files(job.get("output_files"))

    # Verified upload count is the single source of truth for progress.
    rendered_frames = len(output_files)
    if total_frames and total_frames > 0:
        rendered_frames = min(rendered_frames, total_frames)

    actual_gpu = job.get("actual_gpu_name")
    actual_vram = job.get("actual_gpu_vram_gb")

    if actual_gpu:
        vram_suffix = f" {int(actual_vram)}GB" if actual_vram else ""
        display_gpu = f"Vast {actual_gpu}{vram_suffix}"
        display_vram = actual_vram or (machine.get("gpu_vram_gb", 0) if machine else 0)
    else:
        display_gpu = machine["gpu_model"] if machine else "Unknown"
        display_vram = machine.get("gpu_vram_gb", 0) if machine else 0

    return {
        "job_id": job["id"],
        "machine_id": job["machine_id"],
        "machine_type": machine.get("machine_type", "windows") if machine else "windows",
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
    }


def build_dispatch_task_entry(
    planned_task: Any,
    dispatch_result: Any,
    scheduling: dict[str, Any],
) -> dict[str, Any]:
    return {
        "job_id": dispatch_result.job_id,
        "machine_id": planned_task.machine_id,
        "machine_gpu": planned_task.gpu_model,
        "machine_vram": planned_task.gpu_vram_gb,
        "frame_start": planned_task.frame_start,
        "frame_end": planned_task.frame_end,
        "frame_step": planned_task.frame_step,
        "chunk_index": planned_task.chunk_index,
        "total_frames": planned_task.total_frames,
        "rendered_frames": 0,
        "progress_pct": None,
        "status": "pending",
        "power_score": planned_task.power_score,
        "error": None,
        "attempt": 0,
        "max_retries": scheduling.get("max_retries_per_chunk", 0),
        "priority": scheduling.get("priority", 0),
    }
