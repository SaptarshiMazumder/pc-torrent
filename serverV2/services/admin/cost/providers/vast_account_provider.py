"""VastAccountProvider — live Vast.ai prepaid balance (P2).

Read-only, uses the existing ``VAST_API_KEY`` (no new creds).  Surfaces the
remaining prepaid credit so you can see how much Vast headroom is left; the
account read is cached 5 min so the 30s cost poll doesn't hammer the Vast API.

This does NOT report a monthly cost — Vast render spend is already counted
exactly under the ``render`` source (from our own render_telemetry).  This card
is the complementary "money left in the account" view.
"""

from __future__ import annotations

import logging
import time

from serverV2.services.admin.cost.cost_snapshot import (
    CONFIDENCE_EXACT,
    CONFIDENCE_UNAVAILABLE,
    CostSnapshot,
)

log = logging.getLogger(__name__)

_CACHE_TTL_SEC = 300.0


class VastAccountProvider:

    name = "vast_account"

    def __init__(self, vast_client) -> None:
        self._vast = vast_client
        self._cache: tuple[float, dict] | None = None

    def _account(self) -> dict | None:
        now = time.time()
        if self._cache is not None and (now - self._cache[0]) < _CACHE_TTL_SEC:
            return self._cache[1]
        acct = self._vast.get_account()
        if acct is not None:
            self._cache = (now, acct)
        return acct

    def snapshot(self) -> CostSnapshot:
        acct = self._account()
        if not acct:
            return CostSnapshot(
                name=self.name, label="Vast.ai account",
                confidence=CONFIDENCE_UNAVAILABLE,
                note="Vast account read failed (check VAST_API_KEY).",
            )
        credit = acct.get("credit")
        balance = acct.get("balance")
        return CostSnapshot(
            name=self.name,
            label="Vast.ai account (balance)",
            confidence=CONFIDENCE_EXACT,
            usage={
                "credit_usd": credit,
                "balance_usd": balance,
                "email": acct.get("email"),
            },
            note=(
                "Remaining Vast prepaid credit (live).  Render spend itself is "
                "counted exactly under 'GPU render'."
            ),
        )
