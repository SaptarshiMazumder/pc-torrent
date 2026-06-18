"""AllocationCostEstimationConfigRepository -- Firestore-backed.

Reads the ``config/cost_estimation`` document (sibling of
``config/global``).  Unlike ``config/global``, this doc stores its
fields as flat Firestore fields (not a JSON-stringified blob) so
the doc is trivial to edit by hand in the Firestore console.

Missing-doc handling is SOFT here: returns
``AllocationCostEstimationConfig.defaults()`` (feature off) instead
of raising.  That keeps the server booting + every code path safe
even before the doc is created.  Other failure modes (Firestore
unreachable mid-request) still propagate.
"""
from __future__ import annotations

import logging

from firebase_admin import firestore

from serverV2.allocation.allocation_cost_estimation_config import (
    AllocationCostEstimationConfig,
)
from serverV2.infrastructure.auth.firebase_app import init_firebase

log = logging.getLogger(__name__)


_COLLECTION = "config"
_DOC_ID = "cost_estimation"


class AllocationCostEstimationConfigRepository:

    def get(self) -> AllocationCostEstimationConfig:
        """Fetch the Firestore doc and build the config object.
        Returns defaults (feature off) when the doc is missing so
        the server boots cleanly before the doc is created.
        """
        init_firebase()
        client = firestore.client()
        snap = client.collection(_COLLECTION).document(_DOC_ID).get()
        if not snap.exists:
            return AllocationCostEstimationConfig.defaults()
        data = snap.to_dict() or {}
        return AllocationCostEstimationConfig.from_dict(data)
