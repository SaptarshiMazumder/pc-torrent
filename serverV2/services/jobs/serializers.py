"""Pure formatting helpers for standalone job responses."""

from __future__ import annotations

from typing import Any

from serverV2.core.value_objects import (
    compute_progress_pct,
    latest_output_filename,
    output_frame_sort_key,
    parse_output_files,
)


def serialize_job(job: dict[str, Any], machine: dict[str, Any] | None = None) -> dict[str, Any]:
    output_files = parse_output_files(job.get("output_files"))
    rendered = max(0, job.get("rendered_frames") or 0)
    total = job.get("total_frames") or 0
    if total > 0:
        rendered = min(rendered, total)

    return {
        "job_id": job["id"],
        "group_id": job.get("group_id"),
        "machine_id": job.get("machine_id"),
        "machine_type": machine.get("machine_type", "windows") if machine else "windows",
        "machine_gpu": machine.get("gpu_model", "Unknown") if machine else "Unknown",
        "input_filename": job.get("input_filename"),
        "status": job.get("status", "pending"),
        "frame_start": job.get("frame_start"),
        "frame_end": job.get("frame_end"),
        "frame_step": job.get("frame_step") or 1,
        "total_frames": total,
        "rendered_frames": rendered,
        "progress_pct": compute_progress_pct(job.get("status", ""), rendered, total),
        "error": job.get("error"),
        "submitted_at": job.get("submitted_at"),
        "completed_at": job.get("completed_at"),
        "output_files_count": len(output_files),
        "latest_output_file": latest_output_filename(output_files),
    }


def build_output_entries(
    job: dict[str, Any],
    group_id: str | None = None,
) -> list[dict[str, Any]]:
    output_files = parse_output_files(job.get("output_files"))
    sorted_files = sorted(output_files, key=output_frame_sort_key)
    entries = []
    for f in sorted_files:
        entries.append({
            "filename": f,
            "job_id": job["id"],
            "group_id": group_id or job.get("group_id"),
        })
    return entries
