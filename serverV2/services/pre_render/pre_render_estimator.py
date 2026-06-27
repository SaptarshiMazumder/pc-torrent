"""PreRenderEstimator -- stateless cost / wall-time preview.

Frontend already holds the analyzer snapshot (desktop-app local Blender
runs analysis at .blend pick time) and the user's render overrides
(edited in-UI).  POST /pre-render/estimate ships both in the request
body; this estimator merges them via :class:`SceneResolver`, then asks
the orchestrator for a single cost estimate via
``orchestrator.cost_estimate_for_dry_run``.

The orchestrator delegates through allocation_client -> facade ->
planning_service -> planner; the planner stamps per-chunk estimates
that the planning service aggregates into a single
``GroupCostEstimate``.  This estimator turns the result into the
``{wall_time_seconds, cost_credits, machines}`` shape the UI consumes.

No DB reads.  No DB writes.  No render group needs to exist yet.
Errors raise: ``SceneResolutionError`` and ``FrameRangeResolutionError``
both surface bad user input -- callers (the FastAPI router) translate
to HTTP 422.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from serverV2.allocation.services.allocation_planning_service import (
    GroupCostEstimate,
)
from serverV2.config import usd_to_credits
from serverV2.core.value_objects import extract_analysis_warnings
from serverV2.orchestrator.orchestrator import RenderOrchestrator
from serverV2.services.pre_render.frame_range_resolver import resolve_frame_range
from serverV2.services.pre_render.scene_resolver import SceneResolver

log = logging.getLogger(__name__)


class FrameRangeResolutionError(ValueError):
    """Raised when the payload + overrides + analyzer snapshot don't
    yield a usable frame range (no source provided one, or the resolved
    range has zero frames).  Surfaces to the router as HTTP 422."""


@dataclass
class PreRenderEstimateRequest:
    """Inputs the estimator needs.  Constructed in the router from the
    request body -- keeps the estimator decoupled from FastAPI shapes.
    """

    analysis_snapshot: dict[str, Any] = field(default_factory=dict)
    render_overrides: dict[str, Any] = field(default_factory=dict)
    payload_frame_start: int | None = None
    payload_frame_end: int | None = None
    payload_frame_step: int | None = None
    machine_ids: list[str] | None = None
    file_size_bytes: int | None = None
    # Priority drives the cost multiplier applied to the displayed
    # estimate; 1 = NORMAL is the safe default when the client doesn't
    # care.  Same scale as ``ConfirmRenderGroupPayload.priority``.
    priority: int = 1


class PreRenderEstimator:

    def __init__(
        self,
        *,
        orchestrator: RenderOrchestrator,
        scene_resolver: SceneResolver,
        get_credits_per_usd: Callable[[], float],
    ) -> None:
        self._orchestrator = orchestrator
        self._resolver = scene_resolver
        # Live Firestore knob; read per estimate so admin edits to
        # billing.credits_per_usd take effect without a redeploy.
        self._get_credits_per_usd = get_credits_per_usd

    def estimate(
        self, request: PreRenderEstimateRequest,
    ) -> dict[str, Any]:
        """Returns::

            {
                "estimate":   {wall_time_seconds, cost_credits, machines},
                "frame_plan": {frame_start, frame_end, frame_step,
                               total_frames},
                "analysis_warnings": [...],
                "resolved_render_settings": {...},
            }

        Raises ``SceneResolutionError`` or ``FrameRangeResolutionError``
        on bad user input.  Planner-level exceptions propagate as 500s.
        """
        resolved = self._resolver.resolve(
            analysis_snapshot=request.analysis_snapshot,
            render_overrides=request.render_overrides,
            file_size_bytes=request.file_size_bytes,
        )
        heaviness = resolved["heaviness"]
        engine = heaviness["render_engine"]
        normalized_overrides = resolved["render_overrides"]

        plan = resolve_frame_range(
            payload_frame_start=request.payload_frame_start,
            payload_frame_end=request.payload_frame_end,
            payload_frame_step=request.payload_frame_step,
            timeline_overrides=normalized_overrides.get("timeline"),
        )
        if plan is None or plan.total_frames <= 0:
            raise FrameRangeResolutionError(
                "Frame range could not be resolved.  Provide frame_start/"
                "frame_end in the payload, set render_overrides.timeline, "
                "or run blend-file analysis first."
            )

        cost_estimate = self._orchestrator.cost_estimate_for_dry_run(
            frame_start=plan.frame_start,
            frame_end=plan.frame_end,
            frame_step=plan.frame_step,
            total_frames=plan.total_frames,
            engine=engine,
            heaviness=heaviness,
            priority=request.priority,
        )

        # Queue depth at estimate time -- snapshot of what's ahead of
        # the user at each priority level.  Cheap two-query read; same
        # data the daemon ticks against so the number matches reality.
        queue_depth = self._orchestrator.get_queue_depth()

        return {
            "estimate": _to_ui_dto(cost_estimate, self._get_credits_per_usd()),
            "queue_depth": queue_depth,
            "frame_plan": {
                "frame_start": plan.frame_start,
                "frame_end": plan.frame_end,
                "frame_step": plan.frame_step,
                "total_frames": plan.total_frames,
            },
            "analysis_warnings": extract_analysis_warnings(request.analysis_snapshot),
            "resolved_render_settings": normalized_overrides,
        }


def _to_ui_dto(estimate: GroupCostEstimate, credits_per_usd: float) -> dict[str, Any]:
    """Project a ``GroupCostEstimate`` into the UI's display shape.

    The planner's ``total_cost_usd`` is a single deterministic number
    -- no variance modelled, no widening applied.  Output is in
    credits via the wire-boundary conversion.
    """
    cost_credits = usd_to_credits(float(estimate.total_cost_usd), credits_per_usd) or 0.0
    return {
        "wall_time_seconds": int(estimate.wall_time_seconds),
        "cost_credits": round(cost_credits, 2),
        "machines": estimate.chunks,
    }
