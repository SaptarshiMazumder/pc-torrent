"""App-meta routes — public version gate for the desktop client.

GET /app/min-version -> returns ``{min_version, latest_url}`` read
fresh from the Firestore-backed config (no caching, same pattern as
the rest of the allocation flow).

Public, no auth required.  The desktop hits this on launch *before*
the user logs in -- a too-old client can't even reach the login
screen otherwise.

The values flow from ``RenderConfig.desktop`` (see
``serverV2/allocation/render_config.py``):
- ``min_version``: minimum required desktop version.  Older clients
  are force-blocked with UpdateRequiredModal.
- ``latest_url``: deep link to the latest installer.  Today this is
  the GitHub Releases stable URL set up by the desktop-release.yml
  workflow.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, HTTPException

from serverV2.config.render_config_repository import (
    RenderConfigRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["app_meta"])

_config_repo: RenderConfigRepository | None = None


def init(config_repo: RenderConfigRepository) -> None:
    global _config_repo
    _config_repo = config_repo


@router.get("/app/min-version")
def get_min_version():
    if _config_repo is None:
        raise HTTPException(500, "RenderConfigRepository not initialized")
    cfg = _config_repo.get()
    return {
        "min_version": cfg.desktop.min_version,
        "latest_url": cfg.desktop.latest_url,
    }


@router.get("/app/web-config")
def get_web_config():
    """Firebase *web* SDK config for the /home dashboard SPA.

    Public by design: Firebase web configs are client-side identifiers,
    not secrets (the same values ship inside every web bundle).  Served
    from the ``FIREBASE_WEB_CONFIG_JSON`` env var so no config is baked
    into the repo; returns ``{"configured": false}`` when unset so the
    SPA can render a setup hint instead of a broken login.
    """
    raw = os.environ.get("FIREBASE_WEB_CONFIG_JSON", "").strip()
    if not raw:
        return {"configured": False}
    try:
        cfg = json.loads(raw)
    except ValueError:
        log.warning("FIREBASE_WEB_CONFIG_JSON is set but not valid JSON")
        return {"configured": False}
    allowed = {
        "apiKey", "authDomain", "projectId", "storageBucket",
        "messagingSenderId", "appId", "measurementId",
    }
    return {
        "configured": True,
        "firebase": {k: v for k, v in cfg.items() if k in allowed},
    }
