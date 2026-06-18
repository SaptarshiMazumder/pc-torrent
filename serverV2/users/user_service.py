"""UserService — pure logic for the users module.

Stateless transformation only: USD-to-credits conversion and the
per-task debit policy.  No I/O lives here; the facade composes this
with ``UserProfileRepository`` to perform the actual write.
"""

from __future__ import annotations

import logging

from serverV2.config import usd_to_credits

log = logging.getLogger(__name__)


class UserService:

    def __init__(self, credits_per_usd: float) -> None:
        self._credits_per_usd = float(credits_per_usd)

    @property
    def credits_per_usd(self) -> float:
        return self._credits_per_usd

    def compute_credit_debit(self, actual_cost_usd: float | None) -> float:
        """Return the credit amount to debit for a task's final cost.

        ``actual_cost_usd`` <= 0 or ``None`` returns ``0.0`` -- the
        facade treats that as a no-op.

        Delegates the multiplication to ``usd_to_credits`` so this
        module shares the single arithmetic source-of-truth with the
        wire-boundary serializer and the pre-render estimator.
        """
        if not actual_cost_usd or actual_cost_usd <= 0:
            return 0.0
        return usd_to_credits(actual_cost_usd, self._credits_per_usd) or 0.0
