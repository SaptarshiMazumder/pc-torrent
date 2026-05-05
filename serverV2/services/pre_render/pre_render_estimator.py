"""PreRenderEstimator -- stateless cost / wall-time preview.

Frontend already holds the analyzer snapshot (desktop-app local Blender
runs analysis at .blend pick time) and the user's render overrides
(edited in-UI).  POST /pre-render/estimate ships both in the request
body; this estimator merges them via :class:`SceneResolver`, then asks
the orchestrator for a per-tier cost estimate by calling
``orchestrator.cost_estimate_for_dry_run`` once per implemented tier.

The orchestrator delegates through allocation_client -> facade ->
planning_service -> planner; the planner stamps per-chunk estimates
that the planning service aggregates into a single
``GroupCostEstimate``.  This estimator turns each tier's estimate into
the ``{wall_time_seconds, cost_low/mid/high_usd, machines}`` shape the
UI consumes.

The low/mid/high cost range is a UI-only widening of the canonical
``total_cost_usd`` -- a fixed +/- spread so the UI can show "expected
+/- N%" without the planner having to model variance internally.

No DB reads.  No DB writes.  No render group needs to exist yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from serverV2.allocation.allocation_strategies.allocation_helpers import (
    allocation_tiers as tiers,
)
from serverV2.allocation.services.allocation_planning_service import (
    GroupCostEstimate,
)
from serverV2.core.value_objects import extract_analysis_warnings
from serverV2.orchestrator.orchestrator import RenderOrchestrator
from serverV2.services.pre_render.frame_range_resolver import resolve_frame_range
from serverV2.services.pre_render.scene_resolver import (
    SceneResolutionError,
    SceneResolver,
)

log = logging.getLogger(__name__)


# Tiers the estimate produces.  Premium is reserved (allocator not
# implemented) -- returned as null so the UI can show "Coming soon".
_PREVIEW_TIERS: tuple[str, ...] = (tiers.ECONOMY, tiers.STANDARD)

# Fixed UI-only widening band around the canonical ``total_cost_usd``.
# Planner produces a single deterministic number; the UI displays a +/-
# spread so users see "expected, optimistic, pessimistic" instead of a
# false-precision single value.  Tweak if the band feels too narrow/
# wide; the planner does NOT model variance.
_COST_WIDENING_BAND: float = 0.25


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


class PreRenderEstimator:

    def __init__(
        self,
        *,
        orchestrator: RenderOrchestrator,
        scene_resolver: SceneResolver,
    ) -> None:
        self._orchestrator = orchestrator
        self._resolver = scene_resolver

    def estimate(
        self, request: PreRenderEstimateRequest,
    ) -> dict[str, Any]:
        """Returns::

            {
                "tiers": {
                    "economy":  {wall_time_seconds, cost_low_usd, cost_mid_usd,
                                 cost_high_usd, machines}  | null,
                    "standard": {...}                                  | null,
                    "premium":  null,
                },
                "frame_plan": {frame_start, frame_end, frame_step,
                               total_frames}                          | null,
                "analysis_warnings": [...],
                "resolved_render_settings": {...},
            }
        """
        try:
            resolved = self._resolver.resolve(
                analysis_snapshot=request.analysis_snapshot,
                render_overrides=request.render_overrides,
                file_size_bytes=request.file_size_bytes,
            )
        except SceneResolutionError as exc:
            log.warning("pre-render estimate: scene resolution failed: %s", exc)
            return {
                "tiers": {t: None for t in (tiers.ECONOMY, tiers.STANDARD, tiers.PREMIUM)},
                "frame_plan": None,
                "analysis_warnings": extract_analysis_warnings(request.analysis_snapshot),
                "resolved_render_settings": None,
                "error": str(exc),
            }

        heaviness = resolved["heaviness"]
        engine = heaviness["render_engine"]
        normalized_overrides = resolved["render_overrides"]

        plan = resolve_frame_range(
            payload_frame_start=request.payload_frame_start,
            payload_frame_end=request.payload_frame_end,
            payload_frame_step=request.payload_frame_step,
            timeline_overrides=normalized_overrides.get("timeline"),
        )

        frame_plan_dto: dict[str, Any] | None = None
        tiers_dto: dict[str, Any]
        if plan is None or plan.total_frames <= 0:
            tiers_dto = {t: None for t in (tiers.ECONOMY, tiers.STANDARD, tiers.PREMIUM)}
        else:
            frame_plan_dto = {
                "frame_start": plan.frame_start,
                "frame_end": plan.frame_end,
                "frame_step": plan.frame_step,
                "total_frames": plan.total_frames,
            }
            tiers_dto = self._estimate_per_tier(
                plan_frame_start=plan.frame_start,
                plan_frame_end=plan.frame_end,
                plan_frame_step=plan.frame_step,
                plan_total_frames=plan.total_frames,
                heaviness=heaviness,
                engine=engine,
            )

        return {
            "tiers": tiers_dto,
            "frame_plan": frame_plan_dto,
            "analysis_warnings": extract_analysis_warnings(request.analysis_snapshot),
            "resolved_render_settings": normalized_overrides,
        }

    def _estimate_per_tier(
        self,
        *,
        plan_frame_start: int,
        plan_frame_end: int,
        plan_frame_step: int,
        plan_total_frames: int,
        heaviness: dict[str, Any],
        engine: str,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for tier in _PREVIEW_TIERS:
            try:
                estimate = self._orchestrator.cost_estimate_for_dry_run(
                    tier=tier,
                    frame_start=plan_frame_start,
                    frame_end=plan_frame_end,
                    frame_step=plan_frame_step,
                    total_frames=plan_total_frames,
                    heaviness=heaviness,
                    engine=engine,
                )
            except Exception as exc:
                log.warning(
                    "pre-render estimate(tier=%s) failed: %s", tier, exc,
                )
                out[tier] = None
                continue
            if estimate.chunks == 0:
                out[tier] = None
                continue
            out[tier] = _to_ui_dto(estimate)
        out[tiers.PREMIUM] = None
        return out


def _to_ui_dto(estimate: GroupCostEstimate) -> dict[str, Any]:
    """Project a ``GroupCostEstimate`` into the UI's per-tier shape.

    The planner's ``total_cost_usd`` is the canonical mid value; we
    widen by a fixed +/- band purely for display so the UI can show a
    plausible-range card instead of a single false-precision number.
    Variance is NOT modelled in the planner.
    """
    mid = float(estimate.total_cost_usd)
    band = _COST_WIDENING_BAND
    return {
        "wall_time_seconds": int(estimate.wall_time_seconds),
        "cost_mid_usd": round(mid, 4),
        "cost_low_usd": round(mid * (1.0 - band), 4),
        "cost_high_usd": round(mid * (1.0 + band), 4),
        "machines": estimate.chunks,
    }
