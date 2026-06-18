"""Head-to-head LLM model comparison for the dinosaur scene.

Pulls the dinosaur scene's heaviness from render_telemetry (matched
by scene_hash), computes observed seconds-per-frame from the same
telemetry rows, then calls AllocationCostLLMCaller against several
OpenAI models in sequence.  Reports each model's formula + the
predicted seconds-per-frame at the observed tunables, side by side.

Read-only.  Does NOT touch the formula cache table.

Usage:
    python -m scripts.compare_cost_estimator_models

Env required:
    DATABASE_URL    Neon connection string (from serverV2/.env)
    OPENAI_API_KEY  Used for every model call (~$0.05-$0.30 each)
"""

from __future__ import annotations

import os
import statistics
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "serverV2" / ".env")

from serverV2.allocation.allocation_cost_estimation_config import (
    AllocationCostEstimationConfig,
)
from serverV2.allocation.allocation_llm_client import AllocationLLMClient
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
# Hash of the dinosaur scene (from the formula cache the user already
# generated).  If telemetry contained a different scene, pass a different
# hash here.
TARGET_SCENE_HASH_PREFIX = "811d41da59ef"

MODELS_TO_TEST = [
    "gpt-4o",
    "gpt-4o-mini",
    "o3-mini",
    "o1",
]
PROVIDER_NAME = "openai"
# -------------------------------------------------------------------------


class _StubConfigRepo:
    def __init__(self, model: str) -> None:
        self._cfg = AllocationCostEstimationConfig(True, PROVIDER_NAME, model)

    def get(self) -> AllocationCostEstimationConfig:
        return self._cfg


def _load_dinosaur_chunks() -> list[dict]:
    rows = query_all(
        """
        SELECT heaviness_json, gpu_type, fleet, chunk_size, seconds_total
        FROM render_telemetry
        WHERE chunk_size > 0 AND seconds_total > 0
        """
    )
    matched = []
    for row in rows:
        h = row["heaviness_json"]
        if not isinstance(h, dict):
            continue
        if build_scene_invariant_hash(h).startswith(TARGET_SCENE_HASH_PREFIX):
            matched.append(row)
    return matched


def _build_caller(model: str) -> AllocationCostLLMCaller:
    api_key = os.environ["OPENAI_API_KEY"]
    registry = LLMProviderRegistry()
    registry.register(OpenAIProvider(api_key=api_key))
    facade = LLMFacade(registry=registry)
    client = AllocationLLMClient(facade=facade)
    return AllocationCostLLMCaller(client=client, config_repo=_StubConfigRepo(model))


def main() -> None:
    chunks = _load_dinosaur_chunks()
    if not chunks:
        sys.exit(
            f"No telemetry chunks match hash prefix {TARGET_SCENE_HASH_PREFIX!r}.  "
            "Check the hash or check that render_telemetry has the scene."
        )

    h = chunks[0]["heaviness_json"]
    samples = int(h.get("samples") or 0)
    res_pct = int(h.get("resolution_percentage") or 100)
    observed_spf = statistics.median(
        float(r["seconds_total"]) / float(r["chunk_size"]) for r in chunks
    )
    gpu = chunks[0]["gpu_type"]
    fleet = chunks[0]["fleet"]

    print(f"Target scene: {len(chunks)} chunks")
    print(
        f"  engine={h.get('render_engine')!r}  "
        f"verts={int(h.get('vertex_count_total') or 0):,}  "
        f"materials={h.get('material_count')}  "
        f"shader_nodes={h.get('shader_node_count_total')}"
    )
    print(
        f"  features: subdiv={h.get('uses_subdivision')}  "
        f"displ={h.get('uses_displacement')}  "
        f"particles={h.get('uses_particles')}  "
        f"volumetrics={h.get('uses_volumetrics')}  "
        f"sss={h.get('uses_subsurface_scattering')}"
    )
    print(
        f"  observed: {observed_spf:.1f}s/frame at {samples} samples, "
        f"{res_pct}% res, on {gpu!r} ({fleet})\n"
    )

    print(
        f"{'model':<14} | {'base@anchor':>12} | {'sexp':>5} | {'rexp':>5} | "
        f"{'pred@user':>10} | {'pred/obs':>9} | notes"
    )
    print("-" * 110)

    for model in MODELS_TO_TEST:
        print(f"calling {model}... ", end="", flush=True)
        try:
            caller = _build_caller(model)
            formula = caller.call(h)
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            continue

        pred_at_user = formula.seconds_per_frame(samples, res_pct, denoise=False)
        ratio = pred_at_user / observed_spf if observed_spf else float("inf")
        notes = formula.notes[:50] + ("..." if len(formula.notes) > 50 else "")
        print()
        print(
            f"{model:<14} | "
            f"{formula.base_seconds_per_frame_at_anchor:>11.1f}s | "
            f"{formula.samples_exponent:>5.2f} | "
            f"{formula.resolution_exponent:>5.2f} | "
            f"{pred_at_user:>9.1f}s | "
            f"{ratio:>8.2f}x | "
            f"{notes}"
        )

    print()
    print(
        f"observed ref: {observed_spf:.1f}s/frame on {gpu} -- "
        f"predicted is on anchor GPU (RTX 3090), so ratio should be "
        f"~render_speed of {gpu} for a calibrated estimate"
    )


if __name__ == "__main__":
    main()
