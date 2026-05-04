"""PreRenderEstimator — stateless cost / wall-time preview.

Frontend already holds the analyzer snapshot (desktop-app local Blender
runs analysis at .blend pick time) and the user's render overrides
(edited in-UI).  POST /pre-render/estimate ships both in the request
body; this estimator merges them via :class:`SceneResolver`, runs the
existing planning service per implemented tier, and returns the per-tier
``{wall_time_seconds, cost_*_usd, machines}`` shape the UI consumes.

No DB reads.  No DB writes.  No render group needs to exist yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from serverV2.allocation.allocation_strategies.allocation_helpers import (
    allocation_tiers as tiers,
)
from serverV2.allocation.allocation_strategies.analyzers.allocation_cost_analyzer import (
    AllocationMixSlot,
    estimate_cost_for_mix,
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
# implemented) — returned as null so the UI can show "Coming soon".
_PREVIEW_TIERS: tuple[str, ...] = (tiers.ECONOMY, tiers.STANDARD)


@dataclass
class PreRenderEstimateRequest:
    """Inputs the estimator needs.  Constructed in the router from the
    request body — keeps the estimator decoupled from FastAPI shapes.
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
                planned = self._orchestrator.plan(
                    frame_start=plan_frame_start,
                    frame_end=plan_frame_end,
                    frame_step=plan_frame_step,
                    total_frames=plan_total_frames,
                    machine_ids=None,           # no pinning at preview time
                    heaviness=heaviness,
                    engine=engine,
                    tier=tier,
                )
            except Exception as exc:
                log.warning("pre-render estimate(tier=%s) plan failed: %s", tier, exc)
                out[tier] = None
                continue
            if not planned:
                out[tier] = None
                continue
            mix = [
                AllocationMixSlot(
                    render_speed=t.render_speed,
                    price_per_hour=t.price_per_hour,
                    frames_assigned=t.total_frames,
                )
                for t in planned
            ]
            cost = estimate_cost_for_mix(heaviness, mix)
            out[tier] = {
                "wall_time_seconds": int(cost.wall_time_seconds),
                "cost_mid_usd": round(cost.cost_mid_usd, 4),
                "cost_low_usd": round(cost.cost_low_usd, 4),
                "cost_high_usd": round(cost.cost_high_usd, 4),
                "machines": len(planned),
            }
        out[tiers.PREMIUM] = None
        return out
