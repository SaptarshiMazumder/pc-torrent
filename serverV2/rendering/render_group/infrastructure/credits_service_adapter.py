"""Adapter (CROSS-CONTEXT): implements rendering's ``ICreditsService`` by
delegating to the BILLING context's public application use-cases.

This is the concrete FORWARD edge from the bounded-contexts UML.  It lives in
rendering's infrastructure because rendering OWNS the port; it depends on
billing's PUBLIC surface only -- never billing's domain, entities or tables.

The billing use-cases are injected (constructor) by the composition root, the
one place the two contexts meet.  They are typed ``Any`` here purely because
the billing context does not exist yet; at wiring time these become
``billing.application.*`` use-case instances and the ``Any`` tightens up.
"""

from __future__ import annotations

from typing import Any

from serverV2.rendering.render_group.application.ports.credits_service import ChargeOutcome, ICreditsService


class CreditsServiceAdapter(ICreditsService):
    def __init__(
        self,
        *,
        billing_reserve: Any,
        billing_charge: Any,
        billing_refund: Any,
    ) -> None:
        self._billing_reserve = billing_reserve
        self._billing_charge = billing_charge
        self._billing_refund = billing_refund

    def reserve(self, user_id: str, *, estimated_cost_usd: float) -> bool:
        raise NotImplementedError

    def charge(self, user_id: str, *, cost_usd: float) -> ChargeOutcome:
        raise NotImplementedError

    def refund(self, user_id: str, *, cost_usd: float) -> None:
        raise NotImplementedError
