"""Community-fleet routes -- per-env worker image lookup.

GET /community/worker-image -> ``{image}`` from the
``COMMUNITY_WORKER_IMAGE`` env var, resolved once at boot.

Public, no auth required.  The desktop agent (running on a user's PC)
calls this at startup to learn which Docker image to ``docker pull``.
Per-env: each env's serverV2 deployment has its own
``COMMUNITY_WORKER_IMAGE`` (e.g. ``ghcr.io/.../pcrent-community-worker-cycles:dev-v1.2.0``),
so one agent binary can run against any backend and always pull the
right image for that env.

Same shape as the existing Vast/Modal image-tag pattern -- image
metadata sits in env vars alongside ``VAST_DOCKER_IMAGE`` and
``MODAL_WORKER_IMAGE_CYCLES``, NOT in Firestore.

No fallback by design.  Empty env var at boot -> ``init`` raises so
the server fails to start (loud), instead of serving an empty
response to community agents.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["community"])

_worker_image: str = ""


def init(worker_image: str) -> None:
    """Called once at boot with the resolved env-var value.  Raises if
    the value is empty -- the server can't serve community agents
    without a valid image tag, so fail at boot rather than at request
    time."""
    global _worker_image
    if not isinstance(worker_image, str) or not worker_image.strip():
        raise RuntimeError(
            "COMMUNITY_WORKER_IMAGE env var must be set (e.g. "
            "ghcr.io/.../pcrent-community-worker-cycles:dev-v1.2.0).",
        )
    _worker_image = worker_image.strip()


@router.get("/community/worker-image")
def get_worker_image():
    if not _worker_image:
        raise HTTPException(500, "community worker image not initialized")
    return {"image": _worker_image}
