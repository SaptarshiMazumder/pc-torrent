"""AllocationPlanningService -- the cost-intelligence module.

Owns planning (via ``AllocationPlanner`` directly) and cost projection
(via ``AllocationCostAggregator``).  The planner stamps
``estimated_cost_usd`` and ``estimated_seconds`` on every PlannedTask;
this service aggregates them into a group-level GroupCostEstimate --
whether the items came from a just-now planner run (pre-submit
dry-run) or from already-stored ``jobs`` rows (post-submit live group).

After Phase 2b: this service is the SINGLE config reader for the
planning surface.  Every public method calls
``self._config_repo.get()`` exactly once at the start and threads the
relevant slices into the planner (which is pure-functional in its
config inputs).  Dispatch tick AND UI cost-estimate dry-run both land
here, so they always see the same fresh snapshot.

The ``tier`` parameter on the planning surface is accepted for API
compatibility but ignored: cost is no longer a scoring factor and there
is only one strategy.  Tier semantics moved to dispatch-queue priority
(Phase F, future).

Stateless given its constructor deps.
"""

from __future__ import annotations

from typing import Any

from serverV2.allocation.allocation_config_repository import (
    AllocationConfigRepository,
)
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.allocation.allocation_strategies.allocation_planner import (
    AllocationPlanner,
)
from serverV2.allocation.allocation_strategies.analyzers import (
    allocation_time_analyzer,
)
from serverV2.allocation.render_config import RenderConfig
from serverV2.allocation.services.allocation_planning_service.allocation_cost_aggregator import (
    AllocationCostAggregator,
)
from serverV2.allocation.services.allocation_planning_service.group_cost_estimate import (
    GroupCostEstimate,
)
from serverV2.config import RenderTimeConfig
from serverV2.core.models import AvailableResources, PlannedTask


class AllocationPlanningService:

    def __init__(
        self,
        *,
        planner: AllocationPlanner,
        cost_aggregator: AllocationCostAggregator,
        config_repo: AllocationConfigRepository,
    ) -> None:
        self._planner = planner
        self._cost_aggregator = cost_aggregator
        self._config_repo = config_repo

    # ------------------------------------------------------------------
    # Push the freshly-loaded Firestore RenderConfig into the time
    # analyzer module's calibration globals.  Called at the top of every
    # planning entry point so config edits via the admin UI take effect
    # immediately without a redeploy.  Mirrors the bootstrap-time call
    # that used to seed from config.json -- config.json is now only
    # consulted by the Firestore seeder script (one-shot at deploy).
    # ------------------------------------------------------------------

    def _apply_analyzer_calibration(self, cfg: RenderConfig) -> None:
        rt = cfg.frame_allocation.render_time
        allocation_time_analyzer.configure(RenderTimeConfig(
            baseline_sec_cycles=rt.baseline_sec_cycles,
            baseline_sec_eevee=rt.baseline_sec_eevee,
            factors_cycles=rt.factors_cycles,
            factors_eevee=rt.factors_eevee,
            scene_scaling_cycles=rt.scene_scaling_cycles,
            scene_scaling_eevee=rt.scene_scaling_eevee,
            startup=rt.startup_sec,
        ))
        allocation_time_analyzer.configure_combination(
            secondary_feature_credit=cfg.frame_allocation.weights.secondary_feature_credit,
            heavy_multiplier_cap=cfg.frame_allocation.weights.heavy_multiplier_cap,
        )

    # ------------------------------------------------------------------
    # planning surface (used by AllocationPendingTickProcessor)
    # ------------------------------------------------------------------

    def plan_initial(
        self,
        *,
        tier: str | None = None,    # accepted for compat, ignored
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        engine: str | None = None,
        heaviness: dict | None = None,
    ) -> list[PlannedTask]:
        del tier
        cfg = self._config_repo.get()
        self._apply_analyzer_calibration(cfg)
        return self._planner.plan_initial(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            weights=cfg.frame_allocation.weights,
            startup_buffer_sec=cfg.frame_allocation.startup_buffer_sec,
            vram_fleet_boost=cfg.frame_allocation.vram_fleet_boost,
            engine=engine,
            heaviness=heaviness,
        )

    def plan_retry(
        self,
        *,
        tier: str | None = None,    # accepted for compat, ignored
        chunk_request: AllocationChunkRequest,
        resources: AvailableResources,
        heaviness: dict | None = None,
    ) -> PlannedTask | None:
        del tier
        cfg = self._config_repo.get()
        self._apply_analyzer_calibration(cfg)
        return self._planner.plan_retry(
            chunk_request,
            resources,
            weights=cfg.frame_allocation.weights,
            startup_buffer_sec=cfg.frame_allocation.startup_buffer_sec,
            vram_fleet_boost=cfg.frame_allocation.vram_fleet_boost,
            heaviness=heaviness,
        )

    # ------------------------------------------------------------------
    # cost surface (used by AllocationFacade)
    # ------------------------------------------------------------------

    def cost_for_dry_run(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        engine: str | None = None,
        heaviness: dict | None = None,
        priority: int = 1,
    ) -> GroupCostEstimate:
        # Single config read shared between planning and cost aggregation
        # so both halves of dry-run see the SAME snapshot.
        cfg = self._config_repo.get()
        self._apply_analyzer_calibration(cfg)
        tasks = self._planner.plan_initial(
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            resources=resources,
            weights=cfg.frame_allocation.weights,
            startup_buffer_sec=cfg.frame_allocation.startup_buffer_sec,
            vram_fleet_boost=cfg.frame_allocation.vram_fleet_boost,
            engine=engine,
            heaviness=heaviness,
        )
        items = self._cost_aggregator.from_planned_tasks(
            tasks,
            failure_rate=cfg.frame_allocation.failure_rate,
            priority_multiplier=cfg.frame_allocation.weights.multiplier_for(priority),
        )
        return self._cost_aggregator.aggregate(items)

    def cost_for_committed_jobs(
        self, rows: list[dict[str, Any]],
    ) -> GroupCostEstimate:
        items = self._cost_aggregator.from_jobs_rows(rows)
        return self._cost_aggregator.aggregate(items)
