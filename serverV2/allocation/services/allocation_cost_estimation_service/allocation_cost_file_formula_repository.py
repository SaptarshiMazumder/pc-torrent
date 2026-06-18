"""Postgres-backed cache of LLM-derived per-scene formulas.

Keyed by an sha256 over the scene-INVARIANT subset of heaviness:
polygon count, material/light/texture counts, shader nodes, the
heavy-feature booleans, and the render engine.  Tunables (samples,
resolution_percentage, uses_adaptive_sampling, file_size_bytes) are
deliberately EXCLUDED so re-submitting the same .blend with
different render settings hits the same cached formula -- the
formula already parameterises those.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from serverV2.allocation.services.allocation_cost_estimation_service.allocation_cost_file_formula import (
    AllocationCostFileFormula,
)
from serverV2.infrastructure.db import execute, query_one


# Heaviness keys that uniquely identify the scene's intrinsic
# complexity.  Anything not in this set varies between calls on the
# SAME .blend (samples, resolution, file size) and so MUST stay out
# of the cache key.
_INVARIANT_KEYS: tuple[str, ...] = (
    "render_engine",
    "vertex_count_total",
    "object_count",
    "mesh_count",
    "material_count",
    "texture_count",
    "texture_total_bytes",
    "shader_node_count_total",
    "uses_subdivision",
    "uses_displacement",
    "uses_particles",
    "uses_geometry_nodes",
    "geometry_nodes_complexity",
    "uses_subsurface_scattering",
    "uses_volumetrics",
)


def build_scene_invariant_hash(heaviness: dict[str, Any]) -> str:
    """sha256 over the canonical invariant subset of heaviness.

    Module-level (not private to the repo) so the offline anchor-
    builder script can dedupe telemetry rows by the same key the
    runtime cache uses.  Same input MUST always produce the same
    output -- never reorder ``_INVARIANT_KEYS`` or change the JSON
    serialisation here without flushing ``allocation_cost_file_formulas``.
    """
    invariants = {k: heaviness.get(k) for k in _INVARIANT_KEYS}
    canonical = json.dumps(invariants, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AllocationCostFileFormulaRepository:

    def get(self, heaviness: dict[str, Any]) -> AllocationCostFileFormula | None:
        key = build_scene_invariant_hash(heaviness)
        row = query_one(
            "SELECT formula FROM allocation_cost_file_formulas "
            "WHERE scene_hash = %s",
            (key,),
        )
        if row is None:
            return None
        return _formula_from_dict(row["formula"])

    def put(
        self,
        heaviness: dict[str, Any],
        formula: AllocationCostFileFormula,
    ) -> None:
        key = build_scene_invariant_hash(heaviness)
        execute(
            "INSERT INTO allocation_cost_file_formulas "
            "(scene_hash, formula, llm_provider, llm_model) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (scene_hash) DO UPDATE SET "
            "formula = EXCLUDED.formula, "
            "llm_provider = EXCLUDED.llm_provider, "
            "llm_model = EXCLUDED.llm_model",
            (
                key,
                json.dumps(_formula_to_dict(formula)),
                formula.llm_provider,
                formula.llm_model,
            ),
        )


def _formula_to_dict(formula: AllocationCostFileFormula) -> dict[str, Any]:
    return {
        "base_seconds_per_frame_at_anchor": formula.base_seconds_per_frame_at_anchor,
        "samples_exponent": formula.samples_exponent,
        "resolution_exponent": formula.resolution_exponent,
        "denoise_overhead_seconds": formula.denoise_overhead_seconds,
        "engine": formula.engine,
        "notes": formula.notes,
        "llm_provider": formula.llm_provider,
        "llm_model": formula.llm_model,
    }


def _formula_from_dict(raw: Any) -> AllocationCostFileFormula:
    # ``formula`` column is JSONB -- psycopg2 returns a dict directly.
    # Defensive isinstance covers the (currently impossible) TEXT case.
    if isinstance(raw, str):
        raw = json.loads(raw)
    return AllocationCostFileFormula(
        base_seconds_per_frame_at_anchor=float(raw["base_seconds_per_frame_at_anchor"]),
        samples_exponent=float(raw["samples_exponent"]),
        resolution_exponent=float(raw["resolution_exponent"]),
        denoise_overhead_seconds=float(raw["denoise_overhead_seconds"]),
        engine=str(raw["engine"]),
        notes=str(raw.get("notes", "")),
        llm_provider=str(raw["llm_provider"]),
        llm_model=str(raw["llm_model"]),
    )
