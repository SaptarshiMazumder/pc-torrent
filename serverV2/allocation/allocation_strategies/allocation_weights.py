"""AllocationWeights -- per-tier knobs for the unified allocation algorithm.

The allocation core (``AllocationPlanner``) is a single pure-functional
pipeline that takes a scene's heaviness profile, the available targets,
and one ``AllocationWeights`` instance.  Every tier (Economy, Standard,
Premium) is a thin shell that holds one of the presets below and feeds
it into the planner.

Two layers, no extra design pattern beyond Strategy at the outer level
and pure-function composition inside:

  * Outer  -- ``EconomyStrategy`` / ``StandardStrategy`` / ``PremiumStrategy``
    each carry one of the three presets.
  * Inner  -- the planner reads every knob it needs from the weights
    object passed in.  No subclassing, no overriding hooks.

If a future tier needs structurally different behaviour (not just
different weights), extend by adding a separate component slot to the
planner -- not by subclassing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AllocationWeights:
    """One knob bundle that fully describes a tier's behaviour.

    All ``*_weight`` fields are unit-less ratios -- they appear together
    in a linear combination inside the composite scorer.  Caller's
    responsibility to keep ``speed_weight + cost_weight`` reasonable
    (1.0 by convention; the absolute scale doesn't matter).
    """

    # --- Composite scoring --------------------------------------------
    speed_weight: float
    cost_weight: float

    # --- Mix selection -----------------------------------------------
    max_targets: int                 # cap on parallel containers per render
    min_frames_per_chunk: int        # floor for chunk size (per-chunk startup tax)
    fleet_diversification_cap: float # max fraction of picks any single fleet may hold
    # Per-gpu-type cap inside each serverless fleet -- prevents the
    # planner from packing every chunk onto the highest-scoring single
    # gpu_type (e.g. all 12 chunks on RTX 4080S).  Two reasons:
    #   1. Vast supply per gpu_type is finite -- 22 RTX 4090 offers,
    #      7 RTX A5000 offers, etc.  Concentrating risks not actually
    #      being able to dispatch all picks.
    #   2. Failure correlation -- a Vast-side OCI/driver issue on one
    #      gpu_type would take out every chunk planned to it.
    # Community machines have unique ids so this cap is a no-op for them.
    gpu_type_diversification_cap: float

    # --- VRAM feasibility filter --------------------------------------
    # Multiplier applied to ``estimate_required_vram_gb(heaviness)`` when
    # filtering eligibles.  Tighter (1.10) lets cheap small-VRAM cards in
    # at OOM risk; looser (1.30) only admits cards with comfortable
    # headroom.
    vram_safety_factor: float

    # --- Frame distribution -------------------------------------------
    # ``time_balanced`` is the default and almost always correct: the
    # frame split equalises wall-time across chunks so the slowest GPU
    # doesn't bottleneck the render.  Minimises both wall time AND total
    # cost (no GPU sits idle while a slower one finishes).  Other modes
    # exist as escape hatches and are unused today.
    distribute_by: str = "time_balanced"


# ---------------------------------------------------------------------
# Tier presets
# ---------------------------------------------------------------------
#
# The three tiers differ ONLY in these weights.  All three share the
# same algorithm.  ``min_frames_per_chunk = 4`` everywhere -- below
# that, the per-chunk startup tax (download + BVH + shader compile)
# starts dominating cycle time on every fleet.
# ---------------------------------------------------------------------

ECONOMY = AllocationWeights(
    speed_weight=0.15,
    cost_weight=0.85,
    max_targets=12,
    min_frames_per_chunk=4,
    fleet_diversification_cap=0.70,
    gpu_type_diversification_cap=0.50,    # 12 picks -> max 6 of one gpu_type
    vram_safety_factor=1.10,
)

STANDARD = AllocationWeights(
    speed_weight=0.50,
    cost_weight=0.50,
    max_targets=24,
    min_frames_per_chunk=4,
    fleet_diversification_cap=0.70,
    gpu_type_diversification_cap=0.40,    # 24 picks -> max 9 of one gpu_type
    vram_safety_factor=1.20,
)

PREMIUM = AllocationWeights(
    speed_weight=0.85,
    cost_weight=0.15,
    max_targets=60,
    min_frames_per_chunk=4,
    fleet_diversification_cap=0.85,
    gpu_type_diversification_cap=0.30,    # 60 picks -> max 18 of one gpu_type
    vram_safety_factor=1.30,
)
