"""FirestoreCostProvider — Firebase/Firestore cost, pending exact billing.

Firestore bills per read/write/delete + storage.  We don't count ops today
(that would be a write we haven't added) and there's no billing key wired, so
this source is listed as tracked-but-unpriced with a clear note on what unlocks
it: GCP billing (``GCP_BILLING_ACCOUNT``) for exact spend, or op-level counting.
Firestore holds only auth + user/config docs here, so real spend is typically
small.  Read-only.
"""

from __future__ import annotations

import os

from serverV2.services.admin.cost.cost_snapshot import (
    CONFIDENCE_ESTIMATED,
    CONFIDENCE_UNAVAILABLE,
    CostSnapshot,
)


class FirestoreCostProvider:

    name = "firestore"

    def snapshot(self) -> CostSnapshot:
        has_key = bool(os.environ.get("GCP_BILLING_ACCOUNT", "").strip())
        return CostSnapshot(
            name=self.name,
            label="Firestore (auth + config docs)",
            confidence=CONFIDENCE_ESTIMATED if has_key else CONFIDENCE_UNAVAILABLE,
            note=(
                "Firestore stores only auth + user/config docs here — usually a "
                "small bill.  Exact cost needs GCP billing (GCP_BILLING_ACCOUNT); "
                "per-op estimation would need op counting (not enabled)."
            ),
        )
