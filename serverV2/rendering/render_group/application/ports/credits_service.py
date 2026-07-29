"""Port (CROSS-CONTEXT): what rendering needs from the BILLING context.

This is the FORWARD cross-context edge from the bounded-contexts UML.
Rendering owns this abstraction; an adapter in
``rendering/infrastructure/adapters`` implements it by delegating to
billing's PUBLIC application use-cases.  Rendering never imports billing's
domain or touches its ledger tables.

``ChargeOutcome`` is rendering's own view of the result -- deliberately
small.  When credits run dry, ``charged`` is False and the caller reacts;
the deeper "credits exhausted" signal comes back the OTHER way, as a
``CreditsExhausted`` domain event on the shared bus (see the reverse edge).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ChargeOutcome:
    charged: bool
    remaining_credits: float


class ICreditsService(Protocol):
    def reserve(self, user_id: str, *, estimated_cost_usd: float) -> bool: ...

    def charge(self, user_id: str, *, cost_usd: float) -> ChargeOutcome: ...

    def refund(self, user_id: str, *, cost_usd: float) -> None: ...
