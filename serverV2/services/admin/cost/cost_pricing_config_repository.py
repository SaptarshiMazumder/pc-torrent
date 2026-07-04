"""CostPricingConfigRepository — Firestore-backed unit prices.

Reads ``config/cost_pricing`` (sibling of ``config/cost_estimation``), fields
stored flat so they're trivial to edit in the Firestore console.  Fully soft:
returns ``CostPricingConfig.defaults()`` on a missing doc OR any read error —
pricing is advisory, never worth failing an admin read over.  Mirrors
``AllocationCostEstimationConfigRepository``.
"""

from __future__ import annotations

import logging

from serverV2.services.admin.cost.cost_pricing_config import CostPricingConfig

log = logging.getLogger(__name__)

_COLLECTION = "config"
_DOC_ID = "cost_pricing"


class CostPricingConfigRepository:

    def get(self) -> CostPricingConfig:
        try:
            from firebase_admin import firestore

            from serverV2.infrastructure.auth.firebase_app import init_firebase

            init_firebase()
            client = firestore.client()
            snap = client.collection(_COLLECTION).document(_DOC_ID).get()
            if not snap.exists:
                return CostPricingConfig.defaults()
            return CostPricingConfig.from_dict(snap.to_dict() or {})
        except Exception as exc:  # noqa: BLE001 — pricing must never break a read
            log.warning("CostPricingConfig read failed (%s) — using defaults", exc)
            return CostPricingConfig.defaults()
