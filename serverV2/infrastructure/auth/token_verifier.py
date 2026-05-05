"""Token verifier — FastAPI dependency that validates Firebase ID tokens.

Firebase Admin SDK init happens once at boot in ``bootstrap.build``;
this dependency assumes the default app already exists.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPBearer

from firebase_admin import auth

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    creds=Security(_bearer),
) -> dict[str, Any]:
    token: str | None = None
    if creds and getattr(creds, "credentials", None):
        token = creds.credentials
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    try:
        decoded = auth.verify_id_token(token)
    except Exception as exc:
        log.warning("Invalid Firebase token: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {"uid": decoded["uid"], "email": decoded.get("email")}
