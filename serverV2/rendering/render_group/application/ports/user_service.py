"""Port (CROSS-CONTEXT): what rendering needs from the IDENTITY context.

Rendering owns the abstraction; an adapter delegates to identity's public
use-cases.  Rendering holds only a slim local view of a user (``UserRef``) --
the fields it actually needs -- never identity's full ``User`` entity.  That
is the "each context keeps its own model of a shared concept" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class UserRef:
    user_id: str
    email: str


class IUserService(Protocol):
    def get(self, user_id: str) -> UserRef | None: ...
