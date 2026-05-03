"""AllocationStrategySelector — tier + scene profile -> strategy name.

User picks a tier; allocation owns the rest:

  * ECONOMY  -> "economy"
  * STANDARD -> "fast_render" if heavy file (>= 2 GB) OR long render
                (>= 30 frames), else "default"
  * PREMIUM  -> reserved.  Falls back to STANDARD until premium has
                its own allocator.

Lifecycle's only job is forwarding the user's tier; this selector
turns that plus the scene profile into a strategy name that
``AllocationPlanningService`` can dispatch on.
"""

from __future__ import annotations

import logging

from serverV2.allocation.allocation_strategies.allocation_helpers import (
    allocation_tiers as tiers,
)

log = logging.getLogger(__name__)


class AllocationStrategySelector:

    # Heuristic thresholds for picking FastRender over Default within
    # the STANDARD tier.  Either condition (heavy file OR many frames)
    # is enough.  Phase 5 telemetry can move these into config.json.
    _FAST_RENDER_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB
    _FAST_RENDER_TOTAL_FRAMES = 30

    def select_name(
        self,
        *,
        tier: str | None,
        file_size_bytes: int,
        total_frames: int,
    ) -> str:
        resolved = tiers.normalize(tier)
        if resolved == tiers.ECONOMY:
            return "economy"
        if resolved == tiers.PREMIUM:
            log.warning(
                "AllocationStrategySelector: PREMIUM requested but not "
                "implemented; falling back to STANDARD selection",
            )
        # STANDARD (or PREMIUM fallback): pick FastRender for heavy /
        # long renders, Default otherwise.
        size_known = file_size_bytes > 0
        is_heavy = size_known and file_size_bytes >= self._FAST_RENDER_FILE_SIZE_BYTES
        is_long = total_frames >= self._FAST_RENDER_TOTAL_FRAMES
        if is_heavy or is_long:
            return "fast_render"
        return "default"
