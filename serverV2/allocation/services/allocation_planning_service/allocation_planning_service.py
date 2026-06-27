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

from serverV2.config.render_config_repository import (
    RenderConfigRepository,
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
from serverV2.config.render_config import RenderConfig
from serverV2.allocation.services.allocation_cost_estimation_service import (
    AllocationCostEstimationService,
)
from serverV2.allocation.services.allocation_planning_service.allocation_cost_aggregator import (
    AllocationCostAggregator,
)
from serverV2.allocation.services.allocation_planning_service.group_cost_estimate import (
    GroupCostEstimate,
)
from serverV2.config import RenderTimeConfig
from serverV2.core.models import AvailableResources, PlannedTask
from serverV2.llm.llm_exception import LLMException

import logging

_log = logging.getLogger(__name__)


class AllocationPlanningService:

    def __init__(
        self,
        *,
        planner: AllocationPlanner,
        cost_aggregator: AllocationCostAggregator,
        config_repo: RenderConfigRepository,
        cost_estimation_service: AllocationCostEstimationService,
    ) -> None:
        self._planner = planner
        self._cost_aggregator = cost_aggregator
        self._config_repo = config_repo
        self._cost_estimation_service = cost_estimation_service

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
        # LLM-based pre-render cost estimation -- ONLY runs on this
        # path (cost_for_dry_run), never on plan_initial / plan_retry.
        # Feature-flagged via Firestore; flag off = heuristic only.
        # The override travels INSIDE the heaviness dict the analyzer
        # already reads -- no module-level state, no race surface.
        if (
            self._cost_estimation_service.is_enabled()
            and isinstance(heaviness, dict)
        ):
            # LLM is a flaky external boundary (rate limits, the model
            # running out of tokens before emitting the tool call,
            # transient 500s).  Log the failure loudly so the admin
            # sees something to fix, then fall through to the
            # heuristic so the UI still gets an estimate.  Same rollback
            # semantics as flipping ``cost_estimator_enabled`` off.
            try:
                enriched = dict(heaviness)
                enriched["llm_base_seconds_at_anchor"] = (
                    self._cost_estimation_service
                    .base_seconds_per_frame_at_anchor(enriched)
                )
                heaviness = enriched
            except LLMException as exc:
                _log.warning(
                    "LLM cost estimator failed; falling back to "
                    "heuristic for this dry-run: %s", exc,
                )
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
        self,
        rows: list[dict[str, Any]],
        snapshot_usd: float | None = None,
    ) -> GroupCostEstimate:
        """Aggregate the heuristic per-chunk estimates stamped on
        ``jobs`` rows.  When ``snapshot_usd`` is not None, override
        the aggregated ``total_cost_usd`` with the snapshot the
        submit boundary captured -- that's "what the user was
        quoted" (LLM-derived).  Per-chunk breakdown
        (``chunks`` / ``total_seconds`` / ``wall_time_seconds``)
        still comes from the rows so the chunk-detail UI surface
        keeps working.
        """
        items = self._cost_aggregator.from_jobs_rows(rows)
        estimate = self._cost_aggregator.aggregate(items)
        if snapshot_usd is None:
            return estimate
        return GroupCostEstimate(
            chunks=estimate.chunks,
            total_cost_usd=float(snapshot_usd),
            total_seconds=estimate.total_seconds,
            wall_time_seconds=estimate.wall_time_seconds,
        )
