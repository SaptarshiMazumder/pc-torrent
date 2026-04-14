"""Firestore client — user profiles, job/group record sync."""

from __future__ import annotations

import logging
from typing import Any

from firebase_admin import firestore

from serverV2.infrastructure.auth.firebase_app import init_firebase
from serverV2.core.value_objects import now_iso

log = logging.getLogger(__name__)


def _db():
    init_firebase()
    return firestore.client()


def get_user_profile(uid: str) -> dict[str, Any] | None:
    doc = _db().collection("users").document(uid).get()
    return doc.to_dict() if doc.exists else None


def create_user_profile(uid: str, email: str | None) -> dict[str, Any]:
    ts = now_iso()
    profile = {
        "display_name": (email or "").split("@")[0] if email else "",
        "email": email or "",
        "billing_plan": "free",
        "credits": 0,
        "created_at": ts,
        "updated_at": ts,
    }
    _db().collection("users").document(uid).set(profile)
    return profile


def get_or_create_profile(uid: str, email: str | None) -> dict[str, Any]:
    profile = get_user_profile(uid)
    if profile:
        return profile
    return create_user_profile(uid, email)


def update_user_profile(uid: str, updates: dict[str, Any]) -> None:
    updates["updated_at"] = now_iso()
    _db().collection("users").document(uid).update(updates)


def write_job_record(uid: str, job_id: str, data: dict[str, Any]) -> None:
    try:
        _db().collection("users").document(uid).collection("jobs").document(job_id).set(data, merge=True)
    except Exception as exc:
        log.warning("Failed to write Firestore job record %s: %s", job_id, exc)


def write_render_group_record(uid: str, group_id: str, data: dict[str, Any]) -> None:
    try:
        _db().collection("users").document(uid).collection("render_groups").document(group_id).set(data, merge=True)
    except Exception as exc:
        log.warning("Failed to write Firestore group record %s: %s", group_id, exc)
