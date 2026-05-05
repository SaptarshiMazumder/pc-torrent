"""Pre-render routes — stateless RPCs.

These endpoints serve the desktop app between .blend pick and Submit.
The frontend already holds analyzer output and the user's overrides
locally; this layer takes both in the request body, computes, returns.
No persistence.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serverV2.api.dependencies import get_current_user
from serverV2.api.schemas.pre_render import PreRenderEstimatePayload
from serverV2.services.pre_render import (
    PreRenderEstimateRequest,
    PreRenderEstimator,
)
from serverV2.services.pre_render.pre_render_estimator import (
    FrameRangeResolutionError,
)
from serverV2.services.pre_render.scene_resolver import SceneResolutionError

router = APIRouter(tags=["pre_render"])

_estimator: PreRenderEstimator | None = None


def init(estimator: PreRenderEstimator) -> None:
    global _estimator
    _estimator = estimator


def _get() -> PreRenderEstimator:
    if _estimator is None:
        raise HTTPException(500, "PreRenderEstimator not initialized")
    return _estimator


@router.post("/pre-render/estimate")
def estimate(
    payload: PreRenderEstimatePayload,
    user: dict = Depends(get_current_user),
):
    """Cost + wall-time estimate for the configured render.

    Returns ``{estimate, frame_plan, analysis_warnings, resolved_render_settings}``.
    See :meth:`PreRenderEstimator.estimate` for the response shape.

    Maps user-input failures (bad analysis snapshot, unresolvable frame
    range) to HTTP 422.  Planner exceptions bubble as 500s.
    """
    request = PreRenderEstimateRequest(
        analysis_snapshot=payload.analysis_snapshot or {},
        render_overrides=payload.render_overrides or {},
        payload_frame_start=payload.frame_start,
        payload_frame_end=payload.frame_end,
        payload_frame_step=payload.frame_step,
        machine_ids=payload.machine_ids,
        file_size_bytes=payload.file_size_bytes,
    )
    try:
        return _get().estimate(request)
    except (SceneResolutionError, FrameRangeResolutionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
