"""UserProfileRepository — Firestore-backed user profile data access."""

from __future__ import annotations

import logging
from typing import Any

from firebase_admin import firestore

from serverV2.core.value_objects import now_iso
from serverV2.infrastructure.auth.firebase_app import init_firebase

log = logging.getLogger(__name__)


class UserProfileRepository:
    """Reads, writes, and atomically mutates ``users/{uid}`` Firestore docs.

    Source of truth for: tier, credits balance, display_name, email.
    All credit mutations go through ``deduct_credits`` which uses a
    Firestore transaction plus an idempotency marker doc, so a retry
    of the same task can't double-debit.

    Legacy docs predate the ``billing_plan`` -> ``tier`` rename;
    ``get`` surfaces ``billing_plan`` as ``tier`` on read so the API
    contract is stable.  The next write through ``update`` writes only
    ``tier``.
    """

    _COLLECTION = "users"
    _SPEND_SUBCOLLECTION = "spend"

    def _db(self):
        init_firebase()
        return firestore.client()

    def get(self, uid: str) -> dict[str, Any] | None:
        doc = self._db().collection(self._COLLECTION).document(uid).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        if "tier" not in data and "billing_plan" in data:
            data["tier"] = data["billing_plan"]
        return data

    def create_if_missing(self, uid: str, email: str | None) -> dict[str, Any]:
        existing = self.get(uid)
        if existing is not None:
            return existing
        ts = now_iso()
        profile = {
            "display_name": (email or "").split("@")[0] if email else "",
            "email": email or "",
            "tier": "free",
            "credits": 0.0,
            "created_at": ts,
            "updated_at": ts,
        }
        self._db().collection(self._COLLECTION).document(uid).set(profile)
        return profile

    def update(self, uid: str, updates: dict[str, Any]) -> None:
        if not updates:
            return
        payload = dict(updates)
        payload["updated_at"] = now_iso()
        self._db().collection(self._COLLECTION).document(uid).update(payload)

    def record_spend(
        self, uid: str, task_id: str, total_credits_spent: float,
    ) -> float:
        """Set ``users/{uid}/spend/{task_id}.total_credits_spent`` to the
        passed value and atomically deduct the delta against the previously
        recorded total from ``users/{uid}.credits``.

        High-water-mark semantics:  the marker doc holds a running total
        of what this task has cost.  Each call computes
        ``delta = max(0, total_credits_spent - existing)`` and applies
        only that delta to the user's balance.  Repeated calls with the
        same total are no-ops.  Calls with a lower total never refund.

        Returns the delta actually applied (>= 0).  Allows the balance
        to go negative -- no rejection branch.
        """
        if total_credits_spent <= 0:
            return 0.0
        db = self._db()
        user_ref = db.collection(self._COLLECTION).document(uid)
        spend_ref = (
            user_ref
            .collection(self._SPEND_SUBCOLLECTION)
            .document(task_id)
        )

        @firestore.transactional
        def _txn(txn) -> float:
            existing_doc = spend_ref.get(transaction=txn)
            existing_total = float(
                (existing_doc.to_dict() or {}).get("total_credits_spent", 0.0)
            ) if existing_doc.exists else 0.0
            delta = total_credits_spent - existing_total
            if delta <= 0:
                return 0.0
            snap = user_ref.get(transaction=txn)
            ts = now_iso()
            if snap.exists:
                current_balance = float(
                    (snap.to_dict() or {}).get("credits", 0.0)
                )
                txn.update(user_ref, {
                    "credits": current_balance - delta,
                    "updated_at": ts,
                })
            else:
                txn.set(user_ref, {
                    "credits": -delta,
                    "tier": "free",
                    "email": "",
                    "display_name": "",
                    "created_at": ts,
                    "updated_at": ts,
                })
            txn.set(spend_ref, {
                "total_credits_spent": total_credits_spent,
                "updated_at": ts,
            })
            return delta

        return _txn(db.transaction())
