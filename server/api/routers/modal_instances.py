"""
Modal job monitoring endpoints.
Exposes the in-memory per-job state kept by the Modal monitor threads.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/modal", tags=["modal"])


@router.get("/instances")
def list_modal_instances() -> list[dict]:
    """
    Return real-time state for every active Modal job being monitored.
    Each entry contains: job_id, provider_job_id, group_id, machine_id,
    gpu_type, job_status, provider_status, rendered_frames, output_count,
    total_frames, frame_start, frame_end, elapsed_sec, started_at,
    last_poll_at, monitor_action, error, status_history.
    """
    from services import modal
    return modal.get_instance_states()
