"""UserRole — authorization role stamped on a ``users/{uid}`` profile."""

from __future__ import annotations

from enum import Enum


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"

    @classmethod
    def from_value(cls, value: str | None) -> "UserRole":
        """Map a stored role string to a ``UserRole``, defaulting to USER.

        Any missing or unrecognized value resolves to the least-privileged
        role.  Authorization never elevates on bad or absent data -- the
        only way to ADMIN is the exact stored string.
        """
        if value == cls.ADMIN.value:
            return cls.ADMIN
        return cls.USER
