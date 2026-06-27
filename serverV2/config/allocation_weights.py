"""AllocationWeights -- single knob bundle for the unified allocator.

There used to be three tier presets (Economy / Standard / Premium) that
fed different speed-vs-cost weights into a composite scorer.  Cost is no
longer a scoring factor (it bills users at dispatch time, but doesn't
drive *what* gets picked).  Tier semantics moved to dispatch-queue
priority (Phase F, future) -- the allocator no longer branches on tier.

This file is the value-object shape only.  Production values are loaded
from config.json's ``frame_allocation.weights`` block and injected at
boot via :class:`FrameAllocationConfig` -- see ``serverV2.config``.
The dataclass field defaults below are kept so unit tests / smoke
scripts can construct an ``AllocationWeights()`` without going through
the config loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from serverV2.core.value_objects import (
    RENDER_PRIORITY_DEFAULT,
    RENDER_PRIORITY_HIGH,
    RENDER_PRIORITY_LOW,
    RENDER_PRIORITY_NORMAL,
    clamp_render_priority,
)


@dataclass(frozen=True)
class AllocationWeights:
    """Knobs for the unified allocation algorithm.

    Composite-score weights live in 0..1 ratios; their absolute scale
    doesn't matter, only relative ordering across a single scoring pass.
    """

    # --- Composite scoring (speed + CUDA + OS, optional cost) ---------
    # Speed dominates because it directly drives wall time.  CUDA + OS act
    # as tiebreakers among otherwise-similar offers (the 'two RTX 4090
    # offers, one with old drivers' case).
    speed_weight: float = 0.70
    cuda_weight: float = 0.20
    os_weight: float = 0.10
    # Cost-efficiency weight.  Rewards LOW cost-to-render-the-chunk
    # (chunk_seconds * price_per_hour), so a slow-cheap card (e.g. a 4090)
    # can outrank a fast-pricey one (e.g. an H200).  0.0 = cost-blind
    # (default; unchanged behaviour).  Raise it to pull the mix toward
    # $/frame -- tune via the ConfigurationPage like every other weight.
    cost_weight: float = 0.0

    # --- Mix selection -----------------------------------------------
    # Upper bound on parallel containers per render -- NOT a goal.
    # Knapsack rule (startup_amortization_ratio) sets actual K.
    max_targets: int = 60
    # Floor on frames-per-chunk -- below this, per-chunk startup tax
    # dominates total compute time.
    min_frames_per_chunk: int = 4
    # Per-fleet target share of the SERVERLESS slots (community is taken
    # first, separately).  Generic over N fleets: shares are normalized
    # over the fleets that actually have supply (= spill), then slots are
    # apportioned by largest-remainder.  Adding a fleet later = add a key
    # here; no code change in the planner.
    fleet_target_share: dict = field(default_factory=lambda: {
        "modal_serverless": 0.70,
        "vast_serverless": 0.30,
    })
    # Per-(fleet, gpu_type) cap.  Two reasons:
    #   1. Vast supply per gpu_type is finite -- concentrating risks
    #      not actually being able to dispatch all picks.
    #   2. Failure correlation -- a Vast-side OCI/driver issue on one
    #      gpu_type would take out every chunk planned to it.
    gpu_type_diversification_cap: float = 0.40

    # --- VRAM feasibility filter --------------------------------------
    # Multiplier on estimated_required_vram.  Looser (1.10) admits cards
    # at the borderline -- relies on the retry pipeline to recover from
    # the rare OOM.  Tighter (1.30) only admits cards with comfortable
    # headroom.
    vram_safety_factor: float = 1.10

    # --- Knapsack fan-out (the K-decision rule) -----------------------
    # Per-chunk render time must be at least this fraction of the
    # per-chunk startup cost.  Default 0.5 -> render >= 50% of startup,
    # i.e., startup overhead is ~67% of render time per chunk.
    # Lower ratio  -> more chunks, better parallelism, worse amortization
    # Higher ratio -> fewer chunks, more efficient per chunk, slower wall
    startup_amortization_ratio: float = 0.5

    # --- Chunk-count curve (sub-linear growth with frame count) -------
    # Target chunks ~ ceil(sqrt(total_frames * chunk_count_curve)).
    # Replaces the old "always max out frames/min_frames_per_chunk"
    # behaviour that produced too many chunks for medium/large renders.
    # 1.0 -> sqrt(frames) (50f -> 7, 300f -> 18, 1000f -> 32)
    # >1   -> more chunks (more parallelism, more startup overhead)
    # <1   -> fewer chunks (longer per-chunk renders)
    chunk_count_curve: float = 1.0

    # --- Frame distribution -------------------------------------------
    # Time-balanced is the only mode used in production -- frame split
    # equalises wall-time across chunks so the slowest GPU doesn't
    # bottleneck the render.
    distribute_by: str = "time_balanced"

    # --- Time-aware allocation (Phase 5) -----------------------------
    # Filter floor: a target survives the eligibility step only if
    #     available_seconds >= startup_for(target) * time_safety_factor
    # Loose default (1.5) only kicks out the catastrophic mismatch
    # where the window can't even cover startup with a small margin.
    # Real partial-fit handling lives in the headroom factor (below)
    # plus the distribution clamp.
    time_safety_factor: float = 1.5

    # Headroom scoring multiplier in [0, 1].  Applied as:
    #     ratio  = available_seconds / chunk_seconds
    #     factor = min(1.0, ratio / (1 + time_headroom_falloff))
    # Falloff=0.5 gives comfortable headroom (~1.5x chunk) full credit
    # and tighter fits scale linearly toward 0.  None available_seconds
    # short-circuits to factor=1.0 (no penalty for legacy / unknown).
    time_headroom_falloff: float = 0.5

    # --- Heavy-feature combination ------------------------------------
    # How heavy-feature multipliers (texture_factor, shader_factor,
    # feature_factor) combine when a scene has several of them at once.
    #
    # 1.0  -> fully multiplicative (legacy; 5 features at 1.4x compound
    #         to 5.4x even though reality stacks them more like 2x).
    # 0.0  -> "dominant feature only" (the worst feature's multiplier
    #         is the only one that matters; others contribute nothing).
    # 0.3  -> default.  Each non-dominant heavy feature contributes 30%
    #         of its excess above 1.0 on top of the dominant feature.
    #         Matches the empirical observation that real scenes don't
    #         scale multiplicatively across independent features.
    secondary_feature_credit: float = 0.3

    # Hard ceiling on the combined heavy-multiplier so a pathological
    # scene can't blow up the estimate.  Applied AFTER the additive
    # combination above.  0.0 disables the cap.
    heavy_multiplier_cap: float = 5.0

    # --- Priority cost multiplier -------------------------------------
    # Per-priority cost multiplier.  Index = priority level (0 LOW,
    # 1 NORMAL, 2 HIGH).  Applied to BOTH the estimated cost surfaced
    # in the UI and the actual debit billed against the user's credit
    # balance.  Default 1.0 / 1.1 / 1.2 -- HIGH costs 20% more than
    # LOW.  Tunable via ConfigurationPage.
    priority_cost_multipliers: dict = field(default_factory=lambda: {
        "low": 1.0,
        "normal": 1.1,
        "high": 1.2,
    })

    def multiplier_for(self, priority: int) -> float:
        """Look up the cost multiplier for a priority level.  Defaults
        to NORMAL when the input is out of range -- belt-and-braces for
        callers that bypass the API validator.
        """
        p = clamp_render_priority(priority)
        if p == RENDER_PRIORITY_LOW:
            key = "low"
        elif p == RENDER_PRIORITY_HIGH:
            key = "high"
        else:
            key = "normal"
        try:
            return float(self.priority_cost_multipliers.get(key, 1.0))
        except (TypeError, ValueError):
            return 1.0
