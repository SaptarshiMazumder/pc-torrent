"""Cost preview — dry-run dispatch per tier (Phase 9 of tiered_allocation_plan).

Given a render group, asks the orchestrator to ``plan(...)`` once per
implemented tier (Economy / Standard) using current ``AvailableResources``,
then runs the resulting mix through ``cost_analyzer.estimate_cost_for_mix``
to produce a ``$X-$Y`` range plus a wall-time estimate.

The preview is honest by construction: it uses the same code path the
real submit will take.  No DB writes, no dispatch — pure compute.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from serverV2.core.value_objects import parse_analysis_heaviness, parse_json_object
from serverV2.allocation.allocation_strategies.allocation_helpers import allocation_tiers as tiers
from serverV2.allocation.allocation_strategies.analyzers.allocation_cost_analyzer import (
    AllocationMixSlot,
    estimate_cost_for_mix,
)
from serverV2.orchestrator.orchestrator import RenderOrchestrator
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)


# Tiers we currently produce estimates for.  Premium is reserved (UI
# disabled, allocator not built) and returns null so the frontend can
# show "Coming soon".
_PREVIEW_TIERS: tuple[str, ...] = (tiers.ECONOMY, tiers.STANDARD)


def estimate(
    *,
    orchestrator: RenderOrchestrator,
    group_repo: RenderGroupRepository,
    group_id: str,
) -> dict[str, dict[str, Any] | None]:
    """Produce a per-tier cost + wall-time estimate for ``group_id``.

    Output shape::

        {
            "economy":  {wall_time_seconds, cost_low_usd, cost_high_usd,
                          cost_mid_usd, machines, tier_budget_usd},
            "standard": {... same keys ...},
            "premium":  None,    # reserved
        }
    """
    group = group_repo.get_by_id(group_id)
    if not group:
        return {tier: None for tier in (tiers.ECONOMY, tiers.STANDARD, tiers.PREMIUM)}

    # Build the heaviness dict once — same shape lifecycle uses.
    snapshot = parse_json_object(group.get("analysis_snapshot_json"), {})
    heaviness = parse_analysis_heaviness(
        snapshot,
        file_size_bytes=group.get("r2_input_size_bytes"),
    )
    engine = _engine_from_overrides(group.get("render_overrides_json"))

    frame_start = int(group.get("frame_start") or 1)
    frame_end = int(group.get("frame_end") or frame_start)
    frame_step = max(1, int(group.get("frame_step") or 1))
    total_frames = int(group.get("total_frames") or 0)
    if total_frames <= 0 and frame_end >= frame_start:
        total_frames = ((frame_end - frame_start) // frame_step) + 1

    out: dict[str, dict[str, Any] | None] = {}
    for tier in _PREVIEW_TIERS:
        try:
            planned = orchestrator.plan(
                frame_start=frame_start,
                frame_end=frame_end,
                frame_step=frame_step,
                total_frames=total_frames,
                machine_ids=None,                 # don't honour pinning here
                heaviness=heaviness,
                engine=engine,
                tier=tier,
            )
        except Exception as exc:
            log.warning("estimate(tier=%s) plan failed for group %s: %s", tier, group_id, exc)
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

    out[tiers.PREMIUM] = None   # reserved for the future Premium allocator
    return out


def _engine_from_overrides(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    render = parsed.get("render")
    if not isinstance(render, dict):
        return None
    engine = render.get("engine")
    return engine if isinstance(engine, str) else None
