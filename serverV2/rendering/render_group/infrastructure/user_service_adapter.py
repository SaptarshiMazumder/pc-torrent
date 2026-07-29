"""Adapter (CROSS-CONTEXT): implements rendering's ``IUserService`` by
delegating to the IDENTITY context's public application use-cases.

Same shape as the credits adapter -- rendering owns the port, this adapter
translates identity's public result into rendering's slim ``UserRef``.  No
import of identity's ``User`` domain entity.
"""

from __future__ import annotations

from typing import Any

from serverV2.rendering.render_group.application.ports.user_service import IUserService, UserRef


class UserServiceAdapter(IUserService):
    def __init__(self, *, identity_get_user: Any) -> None:
        self._identity_get_user = identity_get_user

    def get(self, user_id: str) -> UserRef | None:
        raise NotImplementedError
