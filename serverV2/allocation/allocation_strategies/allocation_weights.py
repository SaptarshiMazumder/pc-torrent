"""AllocationWeights -- single knob bundle for the unified allocator.

There used to be three tier presets (Economy / Standard / Premium) that
fed different speed-vs-cost weights into a composite scorer.  Cost is no
longer a scoring factor (it bills users at dispatch time, but doesn't
drive *what* gets picked).  Tier semantics moved to dispatch-queue
priority (Phase F, future) -- the allocator no longer branches on tier.

One bundle, one set of knobs.  Constants are tunable; values were chosen
empirically and live here rather than in config.json because they belong
to the algorithm, not the deployment.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AllocationWeights:
    """Knobs for the unified allocation algorithm.

    Composite-score weights live in 0..1 ratios; their absolute scale
    doesn't matter, only relative ordering across a single scoring pass.
    """

    # --- Composite scoring (drops cost; adds CUDA + OS) ---------------
    # Speed dominates because it directly drives wall time and we no
    # longer balance against cost.  CUDA + OS act as tiebreakers among
    # otherwise-similar offers (the 'two RTX 4090 offers, one with old
    # drivers' case).
    speed_weight: float = 0.70
    cuda_weight: float = 0.20
    os_weight: float = 0.10

    # --- Mix selection -----------------------------------------------
    # Upper bound on parallel containers per render -- NOT a goal.
    # Knapsack rule (startup_amortization_ratio) sets actual K.
    max_targets: int = 60
    # Floor on frames-per-chunk -- below this, per-chunk startup tax
    # dominates total compute time.
    min_frames_per_chunk: int = 4
    # Fleet-level diversification cap (community / vast / modal cannot
    # exceed this fraction of picks).  Keeps a single fleet from
    # owning the entire mix.
    fleet_diversification_cap: float = 0.85
    # Per-(fleet, gpu_type) cap.  Two reasons:
    #   1. Vast supply per gpu_type is finite -- concentrating risks
    #      not actually being able to dispatch all picks.
    #   2. Failure correlation -- a Vast-side OCI/driver issue on one
    #      gpu_type would take out every chunk planned to it.
    gpu_type_diversification_cap: float = 0.40

    # --- VRAM feasibility filter --------------------------------------
    # Multiplier on estimated_required_vram.  Tighter (1.10) lets cheap
    # small-VRAM cards in at OOM risk; looser (1.30) only admits cards
    # with comfortable headroom.
    vram_safety_factor: float = 1.20

    # --- Knapsack fan-out (the K-decision rule) -----------------------
    # Per-chunk render time must be at least this fraction of the
    # per-chunk startup cost.  Default 0.5 -> render >= 50% of startup,
    # i.e., startup overhead is ~67% of render time per chunk.
    # Lower ratio  -> more chunks, better parallelism, worse amortization
    # Higher ratio -> fewer chunks, more efficient per chunk, slower wall
    startup_amortization_ratio: float = 0.5

    # --- Frame distribution -------------------------------------------
    # Time-balanced is the only mode used in production -- frame split
    # equalises wall-time across chunks so the slowest GPU doesn't
    # bottleneck the render.
    distribute_by: str = "time_balanced"


# Singleton.  Importing modules use this directly; no presets.
DEFAULT = AllocationWeights()
