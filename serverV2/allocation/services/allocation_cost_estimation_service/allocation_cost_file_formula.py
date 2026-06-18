"""AllocationCostFileFormula -- scene-intrinsic render-time formula.

Frozen value object the LLM emits once per unique .blend.  The
formula is fleet-AGNOSTIC: its output is seconds-per-frame at the
anchor GPU (RTX 3090) at anchor settings (ANCHOR_SAMPLES samples,
ANCHOR_RES_PCT resolution).  ``allocation_time_analyzer`` scales by
the target's ``render_speed`` to get fleet-specific seconds.

The anchor constants are baked into the LLM's system prompt so every
emitted formula sits on the same scale.  Changing the anchors means
invalidating every cached formula (DELETE FROM the storage table) --
deliberately a high-friction operation.
"""
from __future__ import annotations

from dataclasses import dataclass


# Anchor calibration constants -- the units the LLM emits its
# ``base_seconds_per_frame_at_anchor`` against.  These appear verbatim
# in the system prompt; do NOT change without flushing the formula
# storage table.
#
# ANCHOR_SAMPLES is intentionally close to typical user sample counts
# (real-world telemetry sits at 64-250 samples).  Pushing the anchor
# to e.g. 1024 forced the LLM to extrapolate across an order of
# magnitude in samples and it under-predicted by ~5-10x; sitting near
# the data lets it interpolate to nearby anchors instead.
ANCHOR_SAMPLES: int = 128
ANCHOR_RESOLUTION_PCT: int = 100
ANCHOR_GPU: str = "RTX 3090"


@dataclass(frozen=True)
class AllocationCostFileFormula:
    """Per-scene formula parameters emitted by the LLM."""

    base_seconds_per_frame_at_anchor: float
    samples_exponent: float
    resolution_exponent: float
    denoise_overhead_seconds: float
    engine: str
    notes: str
    llm_provider: str
    llm_model: str

    def seconds_per_frame(
        self,
        samples: int,
        resolution_pct: int,
        denoise: bool,
    ) -> float:
        """Scale the anchor measurement to the user's tunables.

        Pure local math; no I/O, no LLM.  Returns seconds-per-frame at
        anchor-GPU scale.  Callers multiply by per-GPU coefficients
        downstream.
        """
        sample_ratio = max(samples, 1) / float(ANCHOR_SAMPLES)
        res_ratio = max(resolution_pct, 1) / float(ANCHOR_RESOLUTION_PCT)
        scaled = (
            self.base_seconds_per_frame_at_anchor
            * (sample_ratio ** self.samples_exponent)
            * (res_ratio ** self.resolution_exponent)
        )
        if denoise:
            scaled += self.denoise_overhead_seconds
        return float(scaled)
