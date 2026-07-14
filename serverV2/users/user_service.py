"""UserService — pure logic for the users module.

Stateless transformation only: USD-to-credits conversion and the
per-task debit policy.  No I/O lives here; the facade composes this
with ``UserProfileRepository`` to perform the actual write.
"""

from __future__ import annotations

import logging
from typing import Callable

from serverV2.config import usd_to_credits

log = logging.getLogger(__name__)


class UserService:

    def __init__(
        self,
        get_credits_per_usd: Callable[[], float],
        get_signup_grant_credits: Callable[[], float],
    ) -> None:
        # Live Firestore knobs (match the get_max_retries pattern): read on
        # every call so admin edits take effect without a redeploy.
        self._get_credits_per_usd = get_credits_per_usd
        self._get_signup_grant_credits = get_signup_grant_credits

    @property
    def credits_per_usd(self) -> float:
        return float(self._get_credits_per_usd())

    def signup_grant_credits(self) -> float:
        """Credit grant for a newly created profile, read live from
        ``config/user_config``.

        A method (not a property) so the facade can hand it to
        ``create_if_missing`` by reference -- the Firestore read is then
        deferred to the actual creation branch and never fires for an
        already-existing profile.
        """
        return float(self._get_signup_grant_credits())

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
        return usd_to_credits(actual_cost_usd, self._get_credits_per_usd()) or 0.0
