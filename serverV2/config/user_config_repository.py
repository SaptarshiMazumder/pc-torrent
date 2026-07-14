"""UserConfigRepository — Firestore-backed read of ``config/user_config``.

Deliberately NOT Redis-mirrored: the only caller is first-time user
profile creation (``UserProfileRepository.create_if_missing``), a cold
path hit roughly once per signup, so there is no read volume to cache.

Missing doc / field -> ``UserConfig`` with a ``0.0`` grant (see
``UserConfig.from_dict``); the value is set by editing Firestore.
"""

from __future__ import annotations

import logging

from firebase_admin import firestore

from serverV2.config.user_config import UserConfig
from serverV2.infrastructure.auth.firebase_app import init_firebase

log = logging.getLogger(__name__)


_COLLECTION = "config"
_DOC_ID = "user_config"


class UserConfigRepository:

    def _db(self):
        init_firebase()
        return firestore.client()

    def get(self) -> UserConfig:
        snap = self._db().collection(_COLLECTION).document(_DOC_ID).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        return UserConfig.from_dict(data)
