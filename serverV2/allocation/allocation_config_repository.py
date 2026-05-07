"""AllocationConfigRepository — Firestore-backed source of truth for
the runtime config blob used by the allocation planner.

The bundled ``serverV2/config.json`` stays in the repo as the canonical
**default** content used by the seed CLI to populate Firestore on first
deploy.  At runtime the server reads from Firestore on every ``get``
call -- never caches.  Failure modes are loud:

* Firestore unreachable     -> raises (caller's planning aborts).
* ``config/global`` missing  -> raises.
* JSON in the doc malformed  -> raises (json.loads).
* JSON missing required keys -> raises (RenderConfig.from_dict).

Returns a typed ``RenderConfig`` whose shape mirrors config.json
exactly, so ``dataclasses.asdict`` round-trips back to the storage
shape.
"""

from __future__ import annotations

import json
import logging

from firebase_admin import firestore

from serverV2.allocation.render_config import RenderConfig
from serverV2.infrastructure.auth.firebase_app import init_firebase

log = logging.getLogger(__name__)


_COLLECTION = "config"
_DOC_ID = "global"
_FIELD = "json"


class AllocationConfigRepository:

    def get(self) -> RenderConfig:
        """Fetch the Firestore doc, parse the JSON blob, build RenderConfig.
        No caching.  Raises on every failure mode listed in the module
        docstring."""
        return RenderConfig.from_dict(self._fetch_dict())

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
        """Write a new config dict to Firestore.  Caller is responsible
        for validation; this method just serializes and stores."""
        init_firebase()
        client = firestore.client()
        client.collection(_COLLECTION).document(_DOC_ID).set(
            {_FIELD: json.dumps(d)},
        )

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
