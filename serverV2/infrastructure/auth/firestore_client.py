"""Firestore client — job/group record sync.

User profile reads/writes moved to
:class:`serverV2.users.user_profile_repository.UserProfileRepository`
in Phase 1 of the user-credits work.  This module now only owns the
``users/{uid}/jobs/{job_id}`` mirror.
"""

from __future__ import annotations

import logging
from typing import Any

from firebase_admin import firestore

from serverV2.infrastructure.auth.firebase_app import init_firebase

log = logging.getLogger(__name__)


def _db():
    init_firebase()
    return firestore.client()


def write_job_record(uid: str, job_id: str, data: dict[str, Any]) -> None:
    try:
        _db().collection("users").document(uid).collection("jobs").document(job_id).set(data, merge=True)
    except Exception as exc:
        log.warning("Failed to write Firestore job record %s: %s", job_id, exc)
