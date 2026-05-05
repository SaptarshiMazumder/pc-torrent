"""AllocationStrategySelector -- tier -> strategy name.

User picks a tier; this maps to one of the three thin-shell strategies
the planning service holds:

  * ECONOMY  -> "economy"
  * STANDARD -> "standard"
  * PREMIUM  -> "premium"

No file-size / frame-count heuristics anymore -- the unified
``AllocationPlanner`` handles small renders and large ones with the
same code, only the weights differ.  STANDARD is the balanced default
for most renders; the user explicitly opts into ECONOMY (cheaper but
slower) or PREMIUM (faster but pricier).
"""

from __future__ import annotations

import logging

from serverV2.allocation.allocation_strategies.allocation_helpers import (
    allocation_tiers as tiers,
)

log = logging.getLogger(__name__)


class AllocationStrategySelector:

    def select_name(
        self,
        *,
        tier: str | None,
        file_size_bytes: int = 0,    # accepted for backwards-compat; ignored
        total_frames: int = 0,        # accepted for backwards-compat; ignored
    ) -> str:
        resolved = tiers.normalize(tier)
        if resolved == tiers.ECONOMY:
            return "economy"
        if resolved == tiers.PREMIUM:
            return "premium"
        return "standard"
