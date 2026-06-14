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

from fastapi import APIRouter, HTTPException

from serverV2.allocation.allocation_config_repository import (
    AllocationConfigRepository,
)

router = APIRouter(tags=["app_meta"])

_config_repo: AllocationConfigRepository | None = None


def init(config_repo: AllocationConfigRepository) -> None:
    global _config_repo
    _config_repo = config_repo


@router.get("/app/min-version")
def get_min_version():
    if _config_repo is None:
        raise HTTPException(500, "AllocationConfigRepository not initialized")
    cfg = _config_repo.get()
    return {
        "min_version": cfg.desktop.min_version,
        "latest_url": cfg.desktop.latest_url,
    }
