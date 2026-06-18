"""Smoke-test the LLM cost estimator against real telemetry.

For the N most-observed scenes in ``render_telemetry``:
  1. Compute the median seconds-per-frame the workers actually
     produced for that scene (at its dominant samples / resolution
     / GPU).
  2. Call the LLM caller FRESH (bypassing the formula cache) on the
     scene's heaviness dict.
  3. Apply the returned formula at the SAME (samples, resolution)
     the observation was at, and compare.

Read-only.  The formula cache table is NOT touched -- we go through
``AllocationCostLLMCaller`` directly, never the orchestrator service
that would write back.

Usage:
    python -m scripts.validate_allocation_cost_estimator

Env required:
    DATABASE_URL       Neon connection string (loaded from serverV2/.env)
    OPENAI_API_KEY     Used to call gpt-4o for each tested scene
"""

from __future__ import annotations

import os
import statistics
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "serverV2" / ".env")

from serverV2.allocation.allocation_llm_client import AllocationLLMClient
from serverV2.allocation.allocation_cost_estimation_config import AllocationCostEstimationConfig
from serverV2.allocation.services.allocation_cost_estimation_service.allocation_cost_file_formula_repository import (
    build_scene_invariant_hash,
)
from serverV2.allocation.services.allocation_cost_estimation_service.allocation_cost_llm_caller import (
    AllocationCostLLMCaller,
)
from serverV2.infrastructure.db import query_all
from serverV2.llm import LLMFacade
from serverV2.llm.llm_provider_registry import LLMProviderRegistry
from serverV2.llm.providers.openai_provider import OpenAIProvider


# ---- Tunables -----------------------------------------------------------
N_SCENES = 5
PROVIDER_NAME = "openai"
MODEL = "gpt-4o"
# -------------------------------------------------------------------------


class _StubConfigRepo:
    """Bypasses Firestore so the script doesn't need init_firebase."""

    def __init__(self) -> None:
        self._cfg = AllocationCostEstimationConfig(
            cost_estimator_enabled=True,
            provider_name=PROVIDER_NAME,
            model=MODEL,
        )

    def get(self) -> AllocationCostEstimationConfig:
        return self._cfg


def _build_llm_caller() -> AllocationCostLLMCaller:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key.startswith("PLACEHOLDER"):
        raise SystemExit(
            "OPENAI_API_KEY missing or still set to the placeholder.  "
            "Fill it in serverV2/.env before running this script."
        )
    registry = LLMProviderRegistry()
    registry.register(OpenAIProvider(api_key=api_key))
    facade = LLMFacade(registry=registry)
    client = AllocationLLMClient(facade=facade)
    return AllocationCostLLMCaller(client=client, config_repo=_StubConfigRepo())


def _load_top_scenes(n: int) -> list[list[dict]]:
    # ``rendered_frames`` is currently always 0 (telemetry writer never
    # fills it); ``chunk_size`` is the real "frames rendered" on
    # completed chunks.  Filter to rows with valid timing data.
    rows = query_all(
        """
        SELECT
            heaviness_json,
            gpu_type,
            fleet,
            chunk_size,
            seconds_total
        FROM render_telemetry
        WHERE chunk_size > 0 AND seconds_total > 0
        """
    )
    by_scene: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        h = row["heaviness_json"]
        if not isinstance(h, dict):
            continue
        by_scene[build_scene_invariant_hash(h)].append(row)
    ranked = sorted(by_scene.values(), key=len, reverse=True)
    return ranked[:n]


def main() -> None:
    caller = _build_llm_caller()
    scene_groups = _load_top_scenes(N_SCENES)
    print(f"Top {len(scene_groups)} scenes by chunk count:\n")

    for i, chunks in enumerate(scene_groups, 1):
        h = chunks[0]["heaviness_json"]
        gpu = chunks[0]["gpu_type"]
        fleet = chunks[0]["fleet"]
        observed_spf = statistics.median(
            float(r["seconds_total"]) / float(r["chunk_size"])
            for r in chunks
        )
        samples = int(h.get("samples", 0) or 0)
        res_pct = int(h.get("resolution_percentage", 100) or 100)
        verts = int(h.get("vertex_count_total", 0) or 0)

        print(f"--- Scene {i} ({len(chunks)} chunks) ---")
        print(
            f"  engine={h.get('render_engine')!r:>18}  "
            f"verts={verts:>12,}  "
            f"materials={h.get('material_count')}  "
            f"shader_nodes={h.get('shader_node_count_total')}"
        )
        print(
            f"  features: volumetrics={h.get('uses_volumetrics')}  "
            f"sss={h.get('uses_subsurface_scattering')}  "
            f"subdiv={h.get('uses_subdivision')}  "
            f"displ={h.get('uses_displacement')}  "
            f"particles={h.get('uses_particles')}  "
            f"geom_nodes={h.get('uses_geometry_nodes')}"
        )
        print(
            f"  observed: {observed_spf:7.1f} s/frame at {samples} samples, "
            f"{res_pct}% res, on {gpu!r} ({fleet})"
        )

        try:
            formula = caller.call(h)
        except Exception as e:
            print(f"  LLM ERROR: {e}\n")
            continue

        predicted_spf = formula.seconds_per_frame(samples, res_pct, denoise=False)
        ratio = predicted_spf / observed_spf if observed_spf else float("inf")
        print(
            f"  formula: base={formula.base_seconds_per_frame_at_anchor:6.1f}s "
            f"@ anchor (1024 samples / 100% / RTX 3090)  "
            f"sexp={formula.samples_exponent:.2f}  rexp={formula.resolution_exponent:.2f}"
        )
        print(
            f"  predicted: {predicted_spf:7.1f} s/frame at {samples} samples, "
            f"{res_pct}% res, on anchor GPU"
        )
        print(
            f"  ratio (predicted_on_anchor / observed_on_{gpu}): "
            f"{ratio:.2f}x"
        )
        print(f"  notes: {formula.notes}")
        print()


if __name__ == "__main__":
    main()
