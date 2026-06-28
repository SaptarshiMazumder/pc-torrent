"""RenderConfigRepository — Firestore-backed source of truth for
the runtime config blob used by the allocation planner.

The bundled ``serverV2/config.json`` stays in the repo as the canonical
**default** content used by the seed CLI to populate Firestore on first
deploy.  Reads go through an optional Redis mirror
(``RenderConfigRedisMirror``) -- a hit returns the cached dict, a miss
falls through to Firestore.  Without the mirror the Firebase Spark
plan's 50K-reads/day per env gets blown by monitor ticks alone.

Failure modes (Firestore path) stay loud:

* Firestore unreachable     -> raises (caller's planning aborts).
* ``config/global`` missing  -> raises.
* JSON in the doc malformed  -> raises (json.loads).
* JSON missing required keys -> raises (RenderConfig.from_dict).

Redis path is silent-fail-open: any Redis problem just causes a
fall-through to Firestore, never an exception.
"""

from __future__ import annotations

import json
import logging

from firebase_admin import firestore

from serverV2.config.render_config import RenderConfig
from serverV2.config.render_config_redis_mirror import (
    RenderConfigRedisMirror,
)
from serverV2.infrastructure.auth.firebase_app import init_firebase

log = logging.getLogger(__name__)


_COLLECTION = "config"
_DOC_ID = "global"
_FIELD = "json"


class RenderConfigRepository:

    def __init__(
        self,
        *,
        redis_mirror: RenderConfigRedisMirror | None = None,
    ) -> None:
        self._mirror = redis_mirror

    def get(self) -> RenderConfig:
        """Return the parsed RenderConfig.  Redis mirror first; on
        miss, fetch from Firestore and warm the mirror for next time."""
        if self._mirror is not None:
            cached = self._mirror.get_cached_dict()
            if cached is not None:
                return RenderConfig.from_dict(cached)
        fresh = self._fetch_dict()
        if self._mirror is not None:
            self._mirror.set_cached_dict(fresh)
        return RenderConfig.from_dict(fresh)

    # ------------------------------------------------------------------
    # admin surface (Phase 3) -- raw dict in / out for UI editing
    # ------------------------------------------------------------------

    def admin_get(self) -> dict:
        """Return the raw config dict from Firestore.  Used by the admin
        endpoint to render the editor; round-trips through ``admin_put``
        without typed-object detours so user formatting choices are
        preserved at the field level."""
        return self._fetch_dict()

    def admin_put(self, d: dict) -> None:
        """Write a new config dict to Firestore + invalidate the Redis
        mirror so the next ``get`` reads the fresh value instead of
        waiting up to one TTL window."""
        init_firebase()
        client = firestore.client()
        client.collection(_COLLECTION).document(_DOC_ID).set(
            {_FIELD: json.dumps(d)},
        )
        if self._mirror is not None:
            self._mirror.invalidate()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _fetch_dict(self) -> dict:
        init_firebase()
        client = firestore.client()
        snap = client.collection(_COLLECTION).document(_DOC_ID).get()
        if not snap.exists:
            raise RuntimeError(
                f"Firestore doc {_COLLECTION}/{_DOC_ID} missing -- "
                "run scripts/seed_config_to_firestore.py to seed it"
            )
        raw = snap.get(_FIELD)
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(
                f"Firestore doc {_COLLECTION}/{_DOC_ID} has no '{_FIELD}' "
                "string field"
            )
        return json.loads(raw)
