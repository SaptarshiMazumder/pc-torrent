"""
Vast.ai instance monitoring endpoints.
Exposes the in-memory per-instance state kept by the polling threads.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/vast", tags=["vast"])


@router.get("/instances")
def list_vast_instances() -> list[dict]:
    """
    Return real-time state for every active Vast.ai instance.
    Each entry contains: job_id, instance_id, group_id, machine_id,
    actual_status, job_status, gpu_model, dph_total, rendered_frames,
    total_frames, frame_start, frame_end, elapsed_sec, started_at,
    last_poll_at, logs, error, status_history.
    """
    from services.vast import get_instance_states
    return get_instance_states()
