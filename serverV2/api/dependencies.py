"""FastAPI dependencies — identity (token) + authorization (role).

``get_current_user`` is pure identity, re-exported from the infrastructure
token verifier.  ``require_admin`` is the authorization layer on top: it
resolves the caller's role through the ``UserFacade`` and rejects non-admins
with 403.  ``init_authz`` wires the facade once at startup (mirrors the
``router.init(...)`` pattern used in ``main._wire_routers``).

Role resolution is cached per-uid in memory (``_ROLE_TTL_SEC``) so the admin
dashboard's frequent polling doesn't do a Firestore read on every request —
without the cache, every ``/admin/*`` call costs one Firestore read.  Because
this is authorization, the TTL is a deliberate tradeoff: revoking someone's
admin role takes up to ``_ROLE_TTL_SEC`` to take effect.  Keep it modest.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Depends, HTTPException

from serverV2.core.user_role import UserRole
from serverV2.infrastructure.auth.token_verifier import get_current_user
from serverV2.users import UserFacade

__all__ = ["get_current_user", "init_authz", "require_admin"]

_user_facade: UserFacade | None = None

# 15 min: roles change rarely, and this is the single biggest Firestore saver
# for the dashboard (one read per admin request otherwise).  Longer = cheaper
# but slower to revoke admin.
_ROLE_TTL_SEC = 900.0
_role_cache: dict[str, tuple[UserRole, float]] = {}


def init_authz(user_facade: UserFacade) -> None:
    global _user_facade
    _user_facade = user_facade


def _resolve_role(uid: str) -> UserRole:
    now = time.time()
    hit = _role_cache.get(uid)
    if hit is not None and hit[1] > now:
        return hit[0]
    if _user_facade is None:
        raise HTTPException(500, "Authorization not initialized")
    role = _user_facade.get_role(uid)
    _role_cache[uid] = (role, now + _ROLE_TTL_SEC)
    return role


def require_admin(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Gate a route to admins.  Verified identity in, role-checked principal out.

    Raises 403 for any non-admin caller.  The returned dict is the identity
    enriched with the resolved ``role``.  Role is cached per-uid for
    ``_ROLE_TTL_SEC`` so polling doesn't hit Firestore every request.
    """
    if _user_facade is None:
        raise HTTPException(500, "Authorization not initialized")
    role = _resolve_role(user["uid"])
    if role != UserRole.ADMIN:
        raise HTTPException(403, "Admin privileges required")
    return {**user, "role": role}
