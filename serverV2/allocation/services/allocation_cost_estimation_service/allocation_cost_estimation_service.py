"""Module entry point -- orchestrates cache lookup + LLM call.

Returns seconds-per-frame at anchor-GPU scale.  The caller
(``AllocationPlanningService.cost_for_dry_run``) stamps the result
into ``heaviness["llm_base_seconds_at_anchor"]`` and passes the
enriched dict through to the planner; ``allocation_time_analyzer``
picks the override up from that field instead of running its
heuristic.  Fleet-specific scaling (``render_speed``) still happens
in the analyzer.
"""
from __future__ import annotations

from typing import Any

from serverV2.allocation.allocation_cost_estimation_config_repository import (
    AllocationCostEstimationConfigRepository,
)
from serverV2.allocation.services.allocation_cost_estimation_service.allocation_cost_file_formula_repository import (
    AllocationCostFileFormulaRepository,
)
from serverV2.allocation.services.allocation_cost_estimation_service.allocation_cost_llm_caller import (
    AllocationCostLLMCaller,
)


class AllocationCostEstimationService:

    def __init__(
        self,
        *,
        repository: AllocationCostFileFormulaRepository,
        llm_caller: AllocationCostLLMCaller,
        config_repo: AllocationCostEstimationConfigRepository,
    ) -> None:
        self._repository = repository
        self._llm_caller = llm_caller
        self._config_repo = config_repo

    def is_enabled(self) -> bool:
        return self._config_repo.get().cost_estimator_enabled

    def base_seconds_per_frame_at_anchor(
        self, heaviness: dict[str, Any],
    ) -> float:
        """Cache-or-LLM lookup + apply tunables + safety multiplier.

        Reads samples / resolution_percentage / uses_adaptive_sampling
        straight from ``heaviness`` -- those are the tunables the
        formula scales over.  Output is at anchor-GPU scale; the
        planner's analyzer divides by ``render_speed`` downstream.

        ``safety_multiplier`` from the Firestore config scales the
        final number.  Applied AFTER the cached formula evaluates
        so changes to the multiplier take effect on the next call
        without flushing the formula cache.
        """
        formula = self._repository.get(heaviness)
        if formula is None:
            formula = self._llm_caller.call(heaviness)
            self._repository.put(heaviness, formula)

        samples = int(heaviness.get("samples") or 0)
        resolution_pct = int(heaviness.get("resolution_percentage") or 100)
        # No explicit "denoise" flag on heaviness today; treat
        # adaptive sampling as the closest signal that an extra
        # post-pass cost may apply.  Tune as the analyzer gains a
        # dedicated denoise field.
        denoise = bool(heaviness.get("uses_adaptive_sampling", False))
        raw = formula.seconds_per_frame(samples, resolution_pct, denoise)
        multiplier = self._config_repo.get().safety_multiplier
        return raw * multiplier
